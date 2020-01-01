import torch
from torch import nn
from torch.autograd import Variable
from torch.nn import CrossEntropyLoss
from transformers.modeling_bert import BertPreTrainedModel, BertModel

from .esim.layers import RNNDropout, Seq2SeqEncoder, SoftmaxAttention
from .esim.utils import replace_masked


class BertForSimMatchModel(BertPreTrainedModel):
    """
    ab、ac交互并编码
    """

    def __init__(self, config):
        super(BertForSimMatchModel, self).__init__(config)
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.seq_relationship = nn.Linear(config.hidden_size, 2)
        self.init_weights()

        self._embedding = self.bert.embeddings.word_embeddings

        self._encoding = Seq2SeqEncoder(nn.LSTM,
                                        config.hidden_size,
                                        config.hidden_size,
                                        bidirectional=True)

        self._rnn_dropout = RNNDropout(p=config.hidden_dropout_prob)
        self._attention = SoftmaxAttention()
        self._projection = nn.Sequential(nn.Linear(4 * 2 * config.hidden_size, config.hidden_size),
                                         nn.ReLU())
        self._composition = Seq2SeqEncoder(nn.LSTM,
                                           config.hidden_size,
                                           config.hidden_size,
                                           bidirectional=True)
        self._classification = nn.Sequential(nn.Dropout(p=config.hidden_dropout_prob),
                                             nn.Linear(4 * 2 * config.hidden_size, config.hidden_size),
                                             nn.Tanh(),
                                             nn.Dropout(p=config.hidden_dropout_prob),
                                             nn.Linear(config.hidden_size, 2))
        self.apply(self.init_esim_weights)

    def forward(self, a, b, c, labels=None, mode="prob"):
        a_mask = a[1].float()
        b_mask = b[1].float()
        c_mask = c[1].float()

        # the parameter is: input_ids, attention_mask, token_type_ids
        # which is corresponding to input_ids, input_mask and segment_ids in InputFeatures
        a_output = self._embedding(a[0])
        b_output = self._embedding(b[0])
        c_output = self._embedding(c[0])
        # The return value: sequence_output, pooled_output, (hidden_states), (attentions)

        v_ab = self.siamese(a_output, b_output, a_mask, b_mask)
        v_ac = self.siamese(a_output, c_output, a_mask, c_mask)

        subtraction = v_ab - v_ac

        # Solution 1: v_ab - v_ac
        # Solution 2: cat(v_ab, v_ac)
        # Solution 3: margin - sim_a + sim_b

        output = self._classification(subtraction)

        if mode == "prob":
            prob = torch.nn.functional.softmax(Variable(output), dim=1)
            return prob
        elif mode == "logits":
            return output
        elif mode == "loss":
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(output.view(-1, 2), labels.view(-1))
            return loss
        elif mode == "evaluate":
            prob = torch.nn.functional.softmax(Variable(output), dim=1)
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(output.view(-1, 2), labels.view(-1))
            return output, prob, loss

    def siamese(self, a_output, b_output, a_mask, b_mask):

        a_length = a_mask.sum(dim=-1).long()
        b_length = b_mask.sum(dim=-1).long()

        a_output = self._encoding(a_output, a_length)
        b_output = self._encoding(b_output, b_length)

        attended_a, attended_b = self._attention(a_output, a_mask, b_output, b_mask)

        enhanced_a = torch.cat([a_output,
                                attended_a,
                                a_output - attended_a,
                                a_output * attended_a],
                               dim=-1)

        enhanced_b = torch.cat([b_output,
                                attended_b,
                                b_output - attended_b,
                                b_output * attended_b],
                               dim=-1)

        projected_a = self._projection(enhanced_a)
        projected_b = self._projection(enhanced_b)

        # projected_ab = self._rnn_dropout(projected_ab)
        # projected_ac = self._rnn_dropout(projected_ac)

        v_ai = self._composition(projected_a, a_length)
        v_bj = self._composition(projected_b, b_length)

        v_a_avg = torch.sum(v_ai * a_mask.unsqueeze(1)
                            .transpose(2, 1), dim=1) / torch.sum(a_mask, dim=1, keepdim=True)
        v_b_avg = torch.sum(v_bj * b_mask.unsqueeze(1)
                            .transpose(2, 1), dim=1) / torch.sum(b_mask, dim=1, keepdim=True)

        v_a_max, _ = replace_masked(v_ai, a_mask, -1e7).max(dim=1)
        v_b_max, _ = replace_masked(v_bj, b_mask, -1e7).max(dim=1)

        v = torch.cat([v_a_avg, v_a_max, v_b_avg, v_b_max], dim=1)

        return v

    @staticmethod
    def init_esim_weights(module):
        if isinstance(module, nn.Linear):
            nn.init.xavier_uniform_(module.weight.data)
            nn.init.constant_(module.bias.data, 0.0)
        elif isinstance(module, nn.LSTM):
            nn.init.xavier_uniform_(module.weight_ih_l0.data)
            nn.init.orthogonal_(module.weight_hh_l0.data)
            nn.init.constant_(module.bias_ih_l0.data, 0.0)
            nn.init.constant_(module.bias_hh_l0.data, 0.0)
            hidden_size = module.bias_hh_l0.data.shape[0] // 4
            module.bias_hh_l0.data[hidden_size:(2 * hidden_size)] = 1.0
            if module.bidirectional:
                nn.init.xavier_uniform_(module.weight_ih_l0_reverse.data)
                nn.init.orthogonal_(module.weight_hh_l0_reverse.data)
                nn.init.constant_(module.bias_ih_l0_reverse.data, 0.0)
                nn.init.constant_(module.bias_hh_l0_reverse.data, 0.0)
                module.bias_hh_l0_reverse.data[hidden_size:(2 * hidden_size)] = 1.0
