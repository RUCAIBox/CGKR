# -*- coding: utf-8 -*-
# @Time   : 2020/12/28
# @Author : Shanlei Mu
# @Email  : slmu@ruc.edu.cn


import torch
import torch.nn as nn
import numpy as np
import scipy.sparse as sp

from recbole.utils import InputType
from recbole.model.abstract_recommender import KnowledgeRecommender
from recbole.model.loss import BPRLoss, EmbLoss
from recbole.model.init import xavier_normal_initialization


def norm_adj(adj):
    sum_arr = (adj > 0).sum(axis=1)
    diag = np.array(sum_arr.flatten())[0] + 1e-7
    diag = np.power(diag, -0.5)
    d = sp.diags(diag)
    laplace_adj = d * adj * d
    return laplace_adj


def convert_to_tensor(laplace_adj):
    laplace_adj = sp.coo_matrix(laplace_adj)
    index = torch.LongTensor([laplace_adj.row, laplace_adj.col])
    data = torch.FloatTensor(laplace_adj.data)
    sparse_laplace_adj = torch.sparse.FloatTensor(
        index, data, torch.Size(laplace_adj.shape))
    return sparse_laplace_adj


class CGKR(KnowledgeRecommender):

    input_type = InputType.PAIRWISE

    def __init__(self, config, dataset, raw_kg_neighbors):
        super(CGKR, self).__init__(config, dataset)

        # load parameters info
        self.embedding_size = config['embedding_size']
        self.n_kg_layers = config['n_kg_layers']
        self.n_ui_layers = config['n_ui_layers']

        self.cf_pos_flag = config['cf_pos_flag']
        self.cf_neg_flag = config['cf_neg_flag']
        self.max_neighbor_size = config['max_neighbor_size']
        self.cf_loss_function = config['cf_loss_function']
        self.cf_pos_weight = config['cf_pos_weight']
        self.cf_neg_weight = config['cf_neg_weight']

        self.ib_beta = config['ib_beta']

        # load dataset info
        self.ui_graph = dataset.inter_matrix(form='coo').astype(np.float32)

        # generate intermediate data
        self.ui_adj = self.get_ui_adj(self.ui_graph).to(self.device)
        self.kg_adj = self.get_kg_adj(raw_kg_neighbors).to(self.device)

        # define layers and loss
        self.user_embedding = nn.Embedding(self.n_users, self.embedding_size,
                                           padding_idx=0)
        self.entity_embedding = nn.Embedding(self.n_entities, self.embedding_size,
                                             padding_idx=0)
        self.bpr_loss = BPRLoss()
        self.mae_loss = nn.L1Loss()
        self.restore_user_e = None
        self.restore_item_e = None

        # parameters initialization
        self.apply(xavier_normal_initialization)
        self.entity_embedding.weight.data[0].fill_(0)
        self.user_embedding.weight.data[0].fill_(0)

    def get_ui_adj(self, ui_graph):
        ui_graph_t = ui_graph.transpose()
        adj = sp.dok_matrix((self.n_users + self.n_items,
                             self.n_users + self.n_items), dtype=np.float32)
        data_dict = dict(zip(
            zip(ui_graph.row, ui_graph.col + self.n_users), [1] * ui_graph.nnz))
        data_dict.update(dict(zip(
            zip(ui_graph_t.row + self.n_users, ui_graph_t.col), [1] * ui_graph_t.nnz)))
        adj._update(data_dict)
        laplace_adj = norm_adj(adj)
        sparse_laplace_adj = convert_to_tensor(laplace_adj)
        return sparse_laplace_adj

    def get_kg_adj(self, kg_neighbors):
        adj = sp.dok_matrix((self.n_entities, self.n_entities), dtype=np.float32)
        data_row, data_col = [], []
        for i in range(self.n_entities):
            for c in kg_neighbors[i]:
                if c != 0:
                    data_row.append(i)
                    data_col.append(c)
        data_dict = dict(zip(
            zip(data_row, data_col), [1. / self.max_neighbor_size] * len(data_row)))
        adj._update(data_dict)
        sparse_adj = convert_to_tensor(adj)
        return sparse_adj

    @staticmethod
    def through_graph(adj, all_embeddings, n_layers):
        all_embeddings_list = [all_embeddings]
        for layer_idx in range(n_layers):
            all_embeddings = torch.sparse.mm(adj, all_embeddings)
            all_embeddings_list.append(all_embeddings)
        all_embeddings = torch.stack(all_embeddings_list, dim=1)
        all_embeddings = torch.mean(all_embeddings, dim=1)
        return all_embeddings

    def forward(self):
        # through kg
        entity_embeddings = self.entity_embedding.weight
        entity_embeddings = self.through_graph(
            self.kg_adj, entity_embeddings, self.n_kg_layers)

        # through ui-graph
        item_embeddings = entity_embeddings[:self.n_items]
        user_embeddings = self.user_embedding.weight
        ui_embeddings = torch.cat([user_embeddings, item_embeddings], dim=0)
        ui_embeddings = self.through_graph(
            self.ui_adj, ui_embeddings, self.n_ui_layers)
        user_embeddings, _ = torch.split(ui_embeddings, [self.n_users, self.n_items])

        return user_embeddings, entity_embeddings

    def get_batch_neighbors(self, items, kg_neighbors):
        batch_size = items.shape[0]
        entities = [items.unsqueeze(1)]
        for i in range(self.n_kg_layers):
            entities.append(kg_neighbors[entities[i]].view(batch_size, -1))
        return entities

    def get_cf_i_embeddings(self, items, kg_neighbors):
        batch_size = items.shape[0]
        entities = self.get_batch_neighbors(items, kg_neighbors)
        entity_vectors = [self.entity_embedding(i) for i in entities]
        item_embeddings_list = [entity_vectors[0].squeeze()]

        for i in range(self.n_kg_layers):
            entity_vectors_next_iter = []
            for hop in range(self.n_kg_layers - i):
                shape = (batch_size, -1, self.max_neighbor_size, self.embedding_size)
                neighbor_vectors = torch.reshape(entity_vectors[hop + 1], shape)
                neighbor_vectors = torch.mean(neighbor_vectors, dim=2)
                entity_vectors_next_iter.append(neighbor_vectors)
            entity_vectors = entity_vectors_next_iter
            item_embeddings_list.append(entity_vectors[0].squeeze())
        item_embeddings = torch.stack(item_embeddings_list, dim=1)
        item_embeddings = torch.mean(item_embeddings, dim=1)

        return item_embeddings

    def get_bpr_loss(self, pos_scores, neg_scores):
        bpr_loss = self.bpr_loss(pos_scores, neg_scores)
        return bpr_loss

    def calculate_loss(self, interaction,
                       user_all_embeddings=None, entity_all_embeddings=None):
        if self.restore_user_e is not None or self.restore_item_e is not None:
            self.restore_user_e, self.restore_item_e = None, None

        users = interaction[self.USER_ID]
        pos_items = interaction[self.ITEM_ID]
        neg_items = interaction[self.NEG_ITEM_ID]

        if user_all_embeddings is None:
            user_all_embeddings, entity_all_embeddings = self.forward()

        u_embeddings = user_all_embeddings[users]
        posi_embeddings = entity_all_embeddings[pos_items]
        negi_embeddings = entity_all_embeddings[neg_items]

        pos_scores = torch.mul(u_embeddings, posi_embeddings).sum(dim=1)
        neg_scores = torch.mul(u_embeddings, negi_embeddings).sum(dim=1)

        # BPR Loss
        bpr_loss = self.get_bpr_loss(pos_scores, neg_scores)

        # CF Loss
        """choice"""
        losses = [bpr_loss]
        if self.cf_pos_flag:
            cf_posi_embeddings = self.get_cf_i_embeddings(
                pos_items, interaction['cf_pos_kg_neighbors'])
            cf_pos_scores = torch.mul(u_embeddings, cf_posi_embeddings).sum(dim=1)
            if self.cf_loss_function == 'mae':
                cf_pos_loss = self.mae_loss(pos_scores, cf_pos_scores)
            else:
                cf_pos_loss = self.get_bpr_loss(cf_pos_scores, neg_scores)
            losses.append(self.cf_pos_weight * cf_pos_loss)
        if self.cf_neg_flag:
            cf_negi_embeddings = self.get_cf_i_embeddings(
                neg_items, interaction['cf_neg_kg_neighbors'])
            cf_neg_scores = torch.mul(u_embeddings, cf_negi_embeddings).sum(dim=1)
            if self.cf_loss_function == 'mae':
                cf_neg_loss = self.mae_loss(neg_scores, cf_neg_scores)
            else:
                cf_neg_loss = self.get_bpr_loss(pos_scores, cf_neg_scores)
            losses.append(self.cf_neg_weight * cf_neg_loss)

        return tuple(losses)

    def full_sort_predict(self, interaction):
        if self.restore_user_e is None or self.restore_item_e is None:
            self.restore_user_e, restore_entity_e = self.forward()
            self.restore_item_e = restore_entity_e[:self.n_items]

        user = interaction[self.USER_ID]
        # get user embedding from storage variable
        u_embeddings = self.restore_user_e[user]

        # dot with all item embedding to accelerate
        scores = torch.matmul(
            u_embeddings, self.restore_item_e.transpose(0, 1))

        return scores.view(-1)

    def predict(self, interaction):
        pass

    def get_reward1(self, scores1, scores2, embeddings):
        # replace ce loss with bpr loss
        """choice"""
        gamma = 1e-10
        part1 = torch.log(gamma + torch.sigmoid(scores1 - scores2))
        # l2-normalization
        part2 = - torch.norm(embeddings, dim=1, p=2)
        return part1 + self.ib_beta * part2

    def get_reward2(self, scores1, scores2):
        gamma = 1e-10
        reward = torch.log(gamma + torch.sigmoid(scores1 - scores2))
        return reward

    def generate_pos_reward(self, interaction, kg_neighbors1, kg_neighbors2,
                            user_all_embeddings, entity_all_embeddings):
        users = interaction[self.USER_ID]
        pos_items = interaction[self.ITEM_ID]
        neg_items = interaction[self.NEG_ITEM_ID]
        u_embeddings = user_all_embeddings[users]
        posi_embeddings = entity_all_embeddings[pos_items]
        negi_embeddings = entity_all_embeddings[neg_items]
        pos_scores = torch.mul(u_embeddings, posi_embeddings).sum(dim=1)
        neg_scores = torch.mul(u_embeddings, negi_embeddings).sum(dim=1)

        # generate reward1
        cf_posi_embeddings1 = self.get_cf_i_embeddings(pos_items, kg_neighbors1)
        cf_pos_scores1 = torch.mul(u_embeddings, cf_posi_embeddings1).sum(dim=1)
        reward1 = self.get_reward1(cf_pos_scores1, neg_scores, cf_posi_embeddings1)

        # generate reward2
        cf_posi_embeddings2 = self.get_cf_i_embeddings(pos_items, kg_neighbors2)
        cf_pos_scores2 = torch.mul(u_embeddings, cf_posi_embeddings2).sum(dim=1)
        reward2 = self.get_reward2(pos_scores, cf_pos_scores2)

        return reward1, reward2

    def generate_neg_reward(self, interaction, kg_neighbors1, kg_neighbors2,
                            user_all_embeddings, entity_all_embeddings):
        users = interaction[self.USER_ID]
        pos_items = interaction[self.ITEM_ID]
        neg_items = interaction[self.NEG_ITEM_ID]
        u_embeddings = user_all_embeddings[users]
        posi_embeddings = entity_all_embeddings[pos_items]
        negi_embeddings = entity_all_embeddings[neg_items]
        pos_scores = torch.mul(u_embeddings, posi_embeddings).sum(dim=1)
        neg_scores = torch.mul(u_embeddings, negi_embeddings).sum(dim=1)

        # generate reward1
        cf_negi_embeddings1 = self.get_cf_i_embeddings(neg_items, kg_neighbors1)
        cf_neg_scores1 = torch.mul(u_embeddings, cf_negi_embeddings1).sum(dim=1)
        reward1 = self.get_reward1(pos_scores, cf_neg_scores1, cf_negi_embeddings1)

        # generate reward2
        cf_negi_embeddings2 = self.get_cf_i_embeddings(neg_items, kg_neighbors2)
        cf_neg_scores2 = torch.mul(u_embeddings, cf_negi_embeddings2).sum(dim=1)
        reward2 = self.get_reward2(cf_neg_scores2, neg_scores)

        return reward1, reward2
