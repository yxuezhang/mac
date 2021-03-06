import math
import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import copy

import loadFiles as tr
from DMax import DMax



class compAggWikiqa(nn.Module):

    def __init__(self, args):
        super(compAggWikiqa, self).__init__()
        self.task = args.task
        self.learning_rate = args.lr
        self.batch_size = args.batch_size
        self.numWords = args.numWords
        self.dropoutP = args.dropoutP
        self.grad = args.grad
        self.comp_type = args.comp_type
        self.window_sizes = args.window_sizes
        self.window_large = args.window_large
        self.gpu = args.gpu
        self.best_score = 0
        self.optim_state = {"learningRate": self.learning_rate}
        self.tanh = nn.Tanh()
        self.relu = nn.ReLU()
        # define dimensions
        self.emb_dim = args.wvecDim
        self.gru_encoder_dim = args.gru_encoder_dim
        self.gru_agg_dim = args.gru_agg_dim
        # define compare function
        if self.comp_type == "mul":
            self.sim_sg_module = self.new_sim_mul_module()
        else:
            Exception("The word matching method is not provided")

        class TempNet(nn.Module):
            def __init__(self, dim):
                super(TempNet, self).__init__()
                self.layer1 = nn.Linear(dim, 1)
                # self.layer2 = nn.Linear(150,1)
                # self.tanh = nn.Tanh()

            def forward(self, input):
                var1 = self.layer1(input)
                var1 = var1.view(-1)
                # print('var1',var1)
                out = F.log_softmax(var1)
                # print(out)
                return out

        # question vs answer layers
        self.emb_vecs = nn.Embedding(self.numWords, self.emb_dim)
        self.emb_vecs.weight.data = tr.loadVacab2Emb(self.task)
        self.proj_modules = self.new_proj_module()
        self.dropout_modules = nn.Dropout(self.dropoutP)
        # the first GRU layer
        self.gru_encoder = self.bi_GRU()
        self.att_module_master = self.new_att_module()
        self.gru_agg = self.bi_GRU()
        self.soft_module = TempNet(2 * args.gru_dim)
        # answer vs answer layers
        self.aa_proj_modules = self.new_proj_module()
        self.aa_att_module_master = self.new_att_module()
        self.aa_gru_module = self.bi_GRU(args)
        self.rl_soft_module = TempNet(4 * args.gru_dim)
        self.aa_soft_module = TempNet(2*args.gru_dim)
        self.criterion = nn.KLDivLoss()
        # self.cur_trans = nn.Linear(mem_dim,75)
        # self.state_trans = nn.Linear(mem_dim,75)
        # self.cos = nn.CosineSimilarity(dim=1,eps=1e-6)

    def new_proj_module(self):
        emb_dim = self.emb_dim
        mem_dim = self.mem_dim

        class NewProjModule(nn.Module):
            def __init__(self, emb_dim, mem_dim):
                super(NewProjModule, self).__init__()
                self.emb_dim = emb_dim
                self.mem_dim = mem_dim
                self.linear1 = nn.Linear(self.emb_dim, self.mem_dim)
                self.linear2 = nn.Linear(self.emb_dim, self.mem_dim)

            def forward(self, input):
                # why project two times??
                if len(input.size()) == 3:
                    b_size = input.size()[0]
                    input = input.view(-1,input.size()[-1])
                    i = nn.Sigmoid()(self.linear1(input))
                    u = nn.Tanh()(self.linear2(input))
                    out = i.mul(u)
                    out = out.view(b_size, -1, out.size()[-1])
                else:
                    i = nn.Sigmoid()(self.linear1(input))
                    u = nn.Tanh()(self.linear2(input))
                    out = i.mul(u)
                return out

        module = NewProjModule(emb_dim, mem_dim)
        return module

    def new_att_module(self):

        class NewAttModule(nn.Module):
            def __init__(self):
                super(NewAttModule, self).__init__()

            def forward(self, linput, rinput): # linput:question, rinput:answer

                # self.lPad = linput.view(-1, linput.size(0), linput.size(1))
                self.lPad = linput  # self.lPad = Padding(0, 0)(linput) TODO: figureout why padding?
                if len(self.lPad.size()) == 3:
                    # self.lPad = self.lPad.permute(0,2,1)
                    # print('lpad',self.lPad.size())
                    # print('rinput',rinput.permute(0,2,1).size())
                    self.M_r = torch.bmm(self.lPad, rinput.permute(0,2,1))

                    self.alpha = F.softmax(self.M_r.permute(0,2,1))
                    self.Yl = torch.bmm(self.alpha, self.lPad)
                else:
                    # print('lpad',self.lPad.size())
                    b_size = rinput.size()[0]
                    rinput = rinput.view(-1,rinput.size()[2])
                    self.M_r = torch.mm(self.lPad, rinput.t())
                    self.alpha = F.softmax(self.M_r.transpose(0, 1))
                    self.Yl = torch.mm(self.alpha, self.lPad)
                    self.Yl = self.Yl.view(b_size,-1,self.Yl.size()[1])
                    # print('yi',self.Yl.size())
                return self.Yl

        att_module = NewAttModule()
        if getattr(self, "att_module_master", None):
            for (tar_param, src_param) in zip(att_module.parameters(), self.att_module_master.parameters()):
                tar_param.grad.data = src_param.grad.data.clone()
        return att_module

    def bi_GRU(self,args):
        class BiGRU(nn.Module):
            def __init__(self, args):
                super(BiGRU, self).__init__()
                self.args = args
                self.input_size = args.mem_dim
                self.hidden_dim = args.gru_dim
                self.num_layers = args.gru_layers
                # C = args.class_num
                # gru
                self.bigru = nn.GRU(self.input_size,
                                    self.hidden_dim,
                                    dropout=args.dropoutP,
                                    num_layers=self.num_layers,
                                    bidirectional=True)
                # linear
                # self.hidden2label = nn.Linear(self.hidden_dim * 2, C)
                #  dropout
                self.dropout = nn.Dropout(args.dropoutP)
            def forward(self, input):
                # gru
                gru_out, _ = self.bigru(input)
                gru_out = torch.transpose(gru_out, 1, 2)
                gru_out = F.max_pool1d(gru_out, gru_out.size(2)).squeeze(2)
                gru_out = F.tanh(gru_out)
                return gru_out
        gru_module = BiGRU(args=args)
        return gru_module

    def new_sim_mul_module(self):
        class NewSimMulModule(nn.Module):
            def __init__(self):
                super(NewSimMulModule, self).__init__()

            def forward(self, inputa, inputh):  # actually it's a_j vs h_j, element-wise mul
                return inputa.mul(inputh)  # return CMulTable().updateOutput([inputq, inputa])

        return NewSimMulModule()

    def comp_agg(self,data_q,data_as,data_as_len):
        for k in range(len(data_as)):
            if data_as_len[k] < self.window_large:
                data_as_len[k] = self.window_large
        inputs_a_emb = self.emb_vecs.forward(
            Variable(data_as.type(torch.cuda.LongTensor),
                     requires_grad=False))  # TODO: why LongTensor would convert to Float
        inputs_q_emb = self.emb_vecs.forward(Variable(data_q, requires_grad=False))

        inputs_a_emb = self.dropout_modules.forward(inputs_a_emb)
        inputs_q_emb = self.dropout_modules.forward(inputs_q_emb)
        projs_a_emb = self.proj_modules.forward(inputs_a_emb)
        projs_q_emb = self.proj_modules.forward(inputs_q_emb)
        print('q',projs_q_emb.size())
        print('a',projs_a_emb.size())
        if data_q.size()[0] == 1:
            projs_q_emb = projs_q_emb.resize(1, self.mem_dim)
        # question-awared answer representation
        att_output = self.att_module_master.forward(projs_q_emb, projs_a_emb)
        # print('att_output',att_output.size())
        sim_output = self.sim_sg_module.forward(projs_a_emb, att_output)
        # print('sim',sim_output.size())
        # conv_output = self.conv_module.forward(sim_output, data_as_len)
        gru_output = self.gru_module(sim_output)
        soft_output = self.soft_module.forward(gru_output)
        return gru_output, soft_output

    def aa_comp_agg(self,data_q,data_as,data_as_len):
        for k in range(len(data_as)):
            data_as_len[k] = data_as[k].size()[0]
            if data_as_len[k] < self.window_large:
                data_as_len[k] = self.window_large
        inputs_a_emb = self.emb_vecs.forward(
            Variable(data_as.type(torch.cuda.LongTensor),
                     requires_grad=False))  # TODO: why LongTensor would convert to Float
        inputs_q_emb = self.emb_vecs.forward(Variable(data_q, requires_grad=False))

        inputs_a_emb = self.dropout_modules.forward(inputs_a_emb)
        inputs_q_emb = self.dropout_modules.forward(inputs_q_emb)

        projs_a_emb = self.aa_proj_modules.forward(inputs_a_emb)
        projs_q_emb = self.aa_proj_modules.forward(inputs_q_emb)

        # if data_q.size()[0] == 1:
        #     projs_q_emb = projs_q_emb.resize(1, self.mem_dim)
        # question-awared answer representation
        att_output = self.aa_att_module_master.forward(projs_q_emb, projs_a_emb)
        # print('att_output',att_output.size())
        sim_output = self.sim_sg_module.forward(projs_a_emb, att_output)
        # print('sim_output',sim_output.size())
        gru_output = self.aa_gru_module.forward(sim_output)
        # print('gru_output',gru_output.size())
        soft_output = self.aa_soft_module.forward(gru_output)
        return gru_output, soft_output

    def rl_state(self,data_q,data_as,data_as_len,q_a_state, q_a_score):

        q_a_score_np = q_a_score.data.cpu().numpy()
        '''
        # concat the origin texts simply 
        rl_state = model.cur_trans(q_a_state[0].view(1, q_a_state[0].size(0)))
        for k in range(2, len(data_as) + 1):
            pos_a_index = np.argmax(q_a_score_np[0:k])  # positive
            q_cur_state = q_a_state[k - 1].view(1, q_a_state[0].size(0))
            if pos_a_index == k - 1:
                rl_state = torch.cat([rl_state, model.cur_trans(q_cur_state)], 0)
                # print('rl_state',rl_state.size())
            else:
                q_pos_cat = torch.cat([data_q, data_as[pos_a_index]])
                current_answer = []
                current_answer.append(data_as[k - 1])
                q_pos_cur_state, _ = model.forward(q_pos_cat, current_answer)
                rl_state = torch.cat([rl_state, model.state_trans(q_pos_cur_state.view(1, q_a_state[0].size(0)))])
        rl_state = model.tanh(rl_state)
        '''
        # for the first answer
        overall_pos_index = np.argmax(q_a_score_np)
        reference = data_as[overall_pos_index].view(1, data_as.size()[1])
        # print('ref',reference.size())
        # print('data_q',data_q.size())
        # ref_info, _ = self.comp_agg(ref_answer, current_answer,data_as_len)
        # ref_info = ref_info.view(1, q_a_state[0].size(0))
        qa_info = q_a_state[0].view(1, q_a_state[0].size(0))
        for k in range(2, len(data_as) + 1):
            pos_a_index = np.argmax(q_a_score_np[0:k])  # positive
            cur_qa_info = q_a_state[k - 1].view(1, q_a_state[0].size(0))
            if pos_a_index == k - 1:
                cur_reference = data_as[overall_pos_index].view(1,data_as.size()[1])
                reference = torch.cat([reference,cur_reference],0)
                qa_info = torch.cat([qa_info, cur_qa_info], 0)
            else:
                cur_reference = data_as[overall_pos_index].view(1,data_as.size()[1])
                # cur_ref_info, _ = self.aa_comp_agg(ref_answer, current_answer,data_as_len)
                qa_info = torch.cat([qa_info, cur_qa_info], 0)
                reference = torch.cat([reference, cur_reference], 0)
        # rl_state = qa_info + ref_info  # add
        # print('refer',reference.size())
        # print('answer',data_as.size())
        ref_info,_ = self.aa_comp_agg(reference,data_as,data_as_len)
        # print(ref_info.size())
        rl_state = torch.cat([qa_info, ref_info], 1)
        # rl_state = self.relu(self.cur_trans(qa_info) + self.state_trans(ref_info))
        return rl_state

    def forward(self, data_q, data_as,data_as_len):
        # Prepare the data
        for k in range(len(data_as)):
        #     data_as_len[k] = data_as[k].size()[0]
        #     # Set the answer with a length less than 5 to [0,0,0,0,0]
        #     if data_as_len[k] < self.window_large:
                data_as_len[k] = self.window_large
        # compare-aggregate encode
        gru_output,soft_output = self.comp_agg(data_q,data_as,data_as_len)
        # get rl state
        rl_state = self.rl_state(data_q,data_as,data_as_len,gru_output,soft_output)
        new_score = self.rl_soft_module.forward(rl_state)
        return new_score

    def save(self, path, config, result, epoch):
        # print(result)
        assert os.path.isdir(path)
        recPath = path + config.task + str(config.expIdx) + 'Record.txt'

        file = open(recPath, 'a')
        if epoch == 0:
            for name, val in vars(config).items():
                file.write(name + '\t' + str(val) + '\n')
        file.write(config.task + ': ' + str(epoch) + ': ')
        for i, vals in enumerate(result):
            for _, val in enumerate(vals):
                file.write('%s, ' % val)
            # if i == 0:
            #     print("Dev: MAP: %s, MRR: %s" % (vals[0], vals[1]))
            # elif i == 1:
            #     print("Test: MAP: %s, MRR: %s" % (vals[0], vals[1]))
        print()
        file.write('\n')
        file.close()




