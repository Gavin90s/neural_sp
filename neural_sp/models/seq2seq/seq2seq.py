#! /usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2018 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Attention-based RNN sequence-to-sequence model (including CTC)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import logging
import numpy as np
import six
import torch
from torch.autograd import Variable

from neural_sp.models.base import ModelBase
from neural_sp.models.linear import Embedding
from neural_sp.models.linear import LinearND
from neural_sp.models.rnnlm.rnnlm import RNNLM
from neural_sp.models.seq2seq.decoders.attention import AttentionMechanism
from neural_sp.models.seq2seq.decoders.decoder import Decoder
from neural_sp.models.seq2seq.decoders.multihead_attention import MultiheadAttentionMechanism
from neural_sp.models.seq2seq.encoders.frame_stacking import stack_frame
from neural_sp.models.seq2seq.encoders.rnn import RNNEncoder
from neural_sp.models.seq2seq.encoders.splicing import splice
from neural_sp.models.torch_utils import np2var
from neural_sp.models.torch_utils import pad_list

logger = logging.getLogger("training")


class Seq2seq(ModelBase):
    """Attention-based RNN sequence-to-sequence model (including CTC)."""

    def __init__(self, args):

        super(ModelBase, self).__init__()

        # for encoder
        self.input_type = args.input_type
        self.input_dim = args.input_dim
        self.nstacks = args.nstacks
        self.nskips = args.nskips
        self.nsplices = args.nsplices
        self.enc_type = args.enc_type
        self.enc_nunits = args.enc_nunits
        if args.enc_type in ['blstm', 'bgru']:
            self.enc_nunits *= 2
        self.bridge_layer = args.bridge_layer

        # for attention layer
        self.attn_nheads = args.attn_nheads

        # for decoder
        self.vocab = args.vocab
        self.vocab_sub1 = args.vocab_sub1
        self.vocab_sub2 = args.vocab_sub2
        self.blank = 0
        self.unk = 1
        self.sos = 2  # NOTE: the same index as <eos>
        self.eos = 2
        self.pad = 3
        # NOTE: reserved in advance

        # for CTC
        self.ctc_weight = args.ctc_weight
        self.ctc_weight_sub1 = args.ctc_weight_sub1
        self.ctc_weight_sub2 = args.ctc_weight_sub2

        # for backward decoder
        self.fwd_weight = 1 - args.bwd_weight
        self.fwd_weight_sub1 = 1 - args.bwd_weight_sub1
        self.fwd_weight_sub2 = 1 - args.bwd_weight_sub2
        self.bwd_weight = args.bwd_weight
        self.bwd_weight_sub1 = args.bwd_weight_sub1
        self.bwd_weight_sub2 = args.bwd_weight_sub2

        # for the sub tasks
        self.main_weight = 1 - args.sub1_weight - args.sub2_weight
        self.sub1_weight = args.sub1_weight
        self.sub2_weight = args.sub2_weight
        self.mtl_per_batch = args.mtl_per_batch

        # Setting for the CNN encoder
        if args.conv_poolings:
            conv_channels = [int(c) for c in args.conv_channels.split('_')] if len(args.conv_channels) > 0 else []
            conv_kernel_sizes = [[int(c.split(',')[0].replace('(', '')), int(c.split(',')[1].replace(')', ''))]
                                 for c in args.conv_kernel_sizes.split('_')] if len(args.conv_kernel_sizes) > 0 else []
            conv_strides = [[int(c.split(',')[0].replace('(', '')), int(c.split(',')[1].replace(')', ''))]
                            for c in args.conv_strides.split('_')] if len(args.conv_strides) > 0 else []
            conv_poolings = [[int(c.split(',')[0].replace('(', '')), int(c.split(',')[1].replace(')', ''))]
                             for c in args.conv_poolings.split('_')] if len(args.conv_poolings) > 0 else []
        else:
            conv_channels = []
            conv_kernel_sizes = []
            conv_strides = []
            conv_poolings = []

        # Encoder
        self.enc = RNNEncoder(
            input_dim=args.input_dim if args.input_type == 'speech' else args.emb_dim,
            rnn_type=args.enc_type,
            nunits=args.enc_nunits,
            nprojs=args.enc_nprojs,
            nlayers=args.enc_nlayers,
            nlayers_sub1=args.enc_nlayers_sub1,
            nlayers_sub2=args.enc_nlayers_sub2,
            dropout_in=args.dropout_in,
            dropout=args.dropout_enc,
            subsample=[int(s) for s in args.subsample.split('_')],
            subsample_type=args.subsample_type,
            nstacks=args.nstacks,
            nsplices=args.nsplices,
            conv_in_channel=args.conv_in_channel,
            conv_channels=conv_channels,
            conv_kernel_sizes=conv_kernel_sizes,
            conv_strides=conv_strides,
            conv_poolings=conv_poolings,
            conv_batch_norm=args.conv_batch_norm,
            residual=args.enc_residual,
            nin=0,
            layer_norm=args.layer_norm,
            task_specific_layer=args.task_specific_layer)

        # Bridge layer between the encoder and decoder
        if args.enc_type == 'cnn':
            self.bridge = LinearND(self.enc.conv.output_dim, args.dec_nunits,
                                   dropout=args.dropout_enc)
            if self.sub1_weight > 0:
                self.bridge_sub1 = LinearND(self.enc.conv.output_dim, args.dec_nunits,
                                            dropout=args.dropout_enc)
            if self.sub2_weight > 0:
                self.bridge_sub2 = LinearND(self.enc.conv.output_dim, args.dec_nunits,
                                            dropout=args.dropout_enc)
            self.enc_nunits = args.dec_nunits
        elif args.bridge_layer:
            self.bridge = LinearND(self.enc_nunits, args.dec_nunits,
                                   dropout=args.dropout_enc)
            if self.sub1_weight > 0:
                self.bridge_sub1 = LinearND(self.enc_nunits, args.dec_nunits,
                                            dropout=args.dropout_enc)
            if self.sub2_weight > 0:
                self.bridge_sub2 = LinearND(self.enc_nunits, args.dec_nunits,
                                            dropout=args.dropout_enc)
            self.enc_nunits = args.dec_nunits

        # MAIN TASK
        directions = []
        if self.fwd_weight > 0 or self.ctc_weight > 0:
            directions.append('fwd')
        if self.bwd_weight > 0:
            directions.append('bwd')
        for dir in directions:
            if (dir == 'fwd' and args.ctc_weight < 1) or dir == 'bwd':
                # Attention layer
                if args.attn_nheads > 1:
                    attn = MultiheadAttentionMechanism(
                        enc_nunits=self.enc_nunits,
                        dec_nunits=args.dec_nunits,
                        attn_type=args.attn_type,
                        attn_dim=args.attn_dim,
                        sharpening_factor=args.attn_sharpening,
                        sigmoid_smoothing=args.attn_sigmoid,
                        conv_out_channels=args.attn_conv_nchannels,
                        conv_kernel_size=args.attn_conv_width,
                        nheads=args.attn_nheads)
                else:
                    attn = AttentionMechanism(
                        enc_nunits=self.enc_nunits,
                        dec_nunits=args.dec_nunits,
                        attn_type=args.attn_type,
                        attn_dim=args.attn_dim,
                        sharpening_factor=args.attn_sharpening,
                        sigmoid_smoothing=args.attn_sigmoid,
                        conv_out_channels=args.attn_conv_nchannels,
                        conv_kernel_size=args.attn_conv_width,
                        dropout=args.dropout_att)
            else:
                attn = None

            # Cold fusion
            if args.rnnlm_cold_fusion and dir == 'fwd':
                logger.inof('cold fusion')
                raise NotImplementedError()
                # TODO(hirofumi): cold fusion for backward RNNLM
            else:
                args.rnnlm_cold_fusion = False

            # TODO(hirofumi): remove later
            if not hasattr(args, 'focal_loss_weight'):
                args.focal_loss_weight = 0.0
                args.focal_loss_gamma = 2.0

            # Decoder
            dec = Decoder(
                attention=attn,
                sos=self.sos,
                eos=self.eos,
                pad=self.pad,
                enc_nunits=self.enc_nunits,
                rnn_type=args.dec_type,
                nunits=args.dec_nunits,
                nlayers=args.dec_nlayers,
                residual=args.dec_residual,
                emb_dim=args.emb_dim,
                vocab=self.vocab,
                logits_temp=args.logits_temp,
                dropout=args.dropout_dec,
                dropout_emb=args.dropout_emb,
                ss_prob=args.ss_prob,
                lsm_prob=args.lsm_prob,
                layer_norm=args.layer_norm,
                focal_loss_weight=args.focal_loss_weight,
                focal_loss_gamma=args.focal_loss_gamma,
                init_with_enc=args.init_with_enc,
                ctc_weight=self.ctc_weight if dir == 'fwd' else 0,
                ctc_fc_list=[int(fc) for fc in args.ctc_fc_list.split('_')] if len(args.ctc_fc_list) > 0 else [],
                input_feeding=args.input_feeding,
                backward=(dir == 'bwd'),
                rnnlm_cold_fusion=args.rnnlm_cold_fusion,
                cold_fusion=args.cold_fusion,
                internal_lm=args.internal_lm,
                rnnlm_init=args.rnnlm_init,
                lmobj_weight=args.lmobj_weight,
                share_lm_softmax=args.share_lm_softmax,
                global_weight=self.main_weight - self.bwd_weight if dir == 'fwd' else self.bwd_weight,
                mtl_per_batch=args.mtl_per_batch)
            setattr(self, 'dec_' + dir, dec)

        # 1st sub task (only for fwd)
        if self.sub1_weight > 0 or (args.mtl_per_batch and args.dict_sub1):
            if self.ctc_weight_sub1 < args.sub1_weight:
                # Attention layer
                if args.attn_nheads_sub1 > 1:
                    attn_sub1 = MultiheadAttentionMechanism(
                        enc_nunits=self.enc_nunits,
                        dec_nunits=args.dec_nunits,
                        attn_type=args.attn_type,
                        attn_dim=args.attn_dim,
                        sharpening_factor=args.attn_sharpening,
                        sigmoid_smoothing=args.attn_sigmoid,
                        conv_out_channels=args.attn_conv_nchannels,
                        conv_kernel_size=args.attn_conv_width,
                        nheads=args.attn_nheads_sub1)
                else:
                    attn_sub1 = AttentionMechanism(
                        enc_nunits=self.enc_nunits,
                        dec_nunits=args.dec_nunits,
                        attn_type=args.attn_type,
                        attn_dim=args.attn_dim,
                        sharpening_factor=args.attn_sharpening,
                        sigmoid_smoothing=args.attn_sigmoid,
                        conv_out_channels=args.attn_conv_nchannels,
                        conv_kernel_size=args.attn_conv_width,
                        dropout=args.dropout_att)
            else:
                attn_sub1 = None

            # Decoder
            self.dec_fwd_sub1 = Decoder(
                attention=attn_sub1,
                sos=self.sos,
                eos=self.eos,
                pad=self.pad,
                enc_nunits=self.enc_nunits,
                rnn_type=args.dec_type,
                nunits=args.dec_nunits,
                nlayers=args.dec_nlayers,
                residual=args.dec_residual,
                emb_dim=args.emb_dim,
                vocab=self.vocab_sub1,
                logits_temp=args.logits_temp,
                dropout=args.dropout_dec,
                dropout_emb=args.dropout_emb,
                ss_prob=args.ss_prob,
                lsm_prob=args.lsm_prob,
                layer_norm=args.layer_norm,
                focal_loss_weight=args.focal_loss_weight,
                focal_loss_gamma=args.focal_loss_gamma,
                init_with_enc=args.init_with_enc,
                ctc_weight=self.ctc_weight_sub1,
                ctc_fc_list=[int(fc) for fc in args.ctc_fc_list_sub1.split('_')
                             ] if len(args.ctc_fc_list_sub1) > 0 else [],
                input_feeding=args.input_feeding,
                internal_lm=args.internal_lm,
                lmobj_weight=args.lmobj_weight_sub1,
                share_lm_softmax=args.share_lm_softmax,
                global_weight=self.sub1_weight,
                mtl_per_batch=args.mtl_per_batch)

        # 2nd sub task (only for fwd)
        if self.sub2_weight > 1 or (args.mtl_per_batch and args.dict_sub2):
            if self.ctc_weight_sub2 < args.sub2_weight:
                # Attention layer
                if args.attn_nheads_sub2 > 1:
                    attn_sub2 = MultiheadAttentionMechanism(
                        enc_nunits=self.enc_nunits,
                        dec_nunits=args.dec_nunits,
                        attn_type=args.attn_type,
                        attn_dim=args.attn_dim,
                        sharpening_factor=args.attn_sharpening,
                        sigmoid_smoothing=args.attn_sigmoid,
                        conv_out_channels=args.attn_conv_nchannels,
                        conv_kernel_size=args.attn_conv_width,
                        nheads=args.attn_nheads_sub2)
                else:
                    attn_sub2 = AttentionMechanism(
                        enc_nunits=self.enc_nunits,
                        dec_nunits=args.dec_nunits,
                        attn_type=args.attn_type,
                        attn_dim=args.attn_dim,
                        sharpening_factor=args.attn_sharpening,
                        sigmoid_smoothing=args.attn_sigmoid,
                        conv_out_channels=args.attn_conv_nchannels,
                        conv_kernel_size=args.attn_conv_width,
                        dropout=args.dropout_att)
            else:
                attn_sub2 = None

            # Decoder
            self.dec_fwd_sub2 = Decoder(
                attention=attn_sub2,
                sos=self.sos,
                eos=self.eos,
                pad=self.pad,
                enc_nunits=self.enc_nunits,
                rnn_type=args.dec_type,
                nunits=args.dec_nunits,
                nlayers=args.dec_nlayers,
                residual=args.dec_residual,
                emb_dim=args.emb_dim,
                vocab=self.vocab_sub2,
                logits_temp=args.logits_temp,
                dropout=args.dropout_dec,
                dropout_emb=args.dropout_emb,
                ss_prob=args.ss_prob,
                lsm_prob=args.lsm_prob,
                layer_norm=args.layer_norm,
                focal_loss_weight=args.focal_loss_weight,
                focal_loss_gamma=args.focal_loss_gamma,
                init_with_enc=args.init_with_enc,
                ctc_weight=self.ctc_weight_sub2,
                ctc_fc_list=[int(fc) for fc in args.ctc_fc_list_sub2.split('_')
                             ] if len(args.ctc_fc_list_sub2) > 0 else [],
                input_feeding=args.input_feeding,
                internal_lm=args.internal_lm,
                lmobj_weight=args.lmobj_weight_sub1,
                global_weight=self.sub2_weight,
                mtl_per_batch=args.mtl_per_batch)

        if args.input_type == 'text':
            if args.vocab == args.vocab_sub1:
                # Share the embedding layer between input and output
                self.embed_in = dec.embed
            else:
                self.embed_in = Embedding(vocab=args.vocab_sub1,
                                          emb_dim=args.emb_dim,
                                          dropout=args.dropout_emb,
                                          ignore_index=self.pad)

        # Initialize weight matrices
        self.init_weights(args.param_init, dist=args.param_init_dist, ignore_keys=['bias'])

        # Initialize CNN layers like chainer
        self.init_weights(args.param_init, dist='lecun', keys=['conv'], ignore_keys=['score'])

        # Initialize all biases with 0
        self.init_weights(0, dist='constant', keys=['bias'])

        # Recurrent weights are orthogonalized
        if args.rec_weight_orthogonal:
            # encoder
            if args.enc_type != 'cnn':
                self.init_weights(args.param_init, dist='orthogonal',
                                  keys=[args.enc_type, 'weight'], ignore_keys=['bias'])
            # TODO(hirofumi): in case of CNN + LSTM
            # decoder
            self.init_weights(args.param_init, dist='orthogonal',
                              keys=[args.dec_type, 'weight'], ignore_keys=['bias'])

        # Initialize bias in forget gate with 1
        self.init_forget_gate_bias_with_one()

        # Initialize bias in gating with -1
        if args.rnnlm_cold_fusion:
            self.init_weights(-1, dist='constant', keys=['cf_linear_lm_gate.fc.bias'])

    def forward(self, xs, ys, ys_sub1=None, ys_sub2=None, reporter=None,
                task='ys', is_eval=False):
        """Forward computation.

        Args:
            xs (list): A list of length `[B]`, which contains arrays of size `[T, input_dim]`
            ys (list): A list of length `[B]`, which contains arrays of size `[L]`
            ys_sub1 (list): A list of lenght `[B]`, which contains arrays of size `[L_sub1]`
            ys_sub2 (list): A list of lenght `[B]`, which contains arrays of size `[L_sub2]`
            reporter ():
            task (str): ys or ys_sub1 or ys_sub2
            is_eval (bool): the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            loss (FloatTensor): `[1]`
            acc (float): Token-level accuracy in teacher-forcing

        """
        if is_eval:
            self.eval()
            with torch.no_grad():
                loss, observation = self._forward(xs, ys, ys_sub1, ys_sub2, task)
        else:
            self.train()
            loss, observation = self._forward(xs, ys, ys_sub1, ys_sub2, task)

        # Report here
        if reporter is not None:
            reporter.add(observation, is_eval)

        return loss, reporter

    def _forward(self, xs, ys, ys_sub1, ys_sub2, task):
        # Encode input features
        if self.input_type == 'speech':
            if self.mtl_per_batch:
                enc_out, perm_idx = self.encode(xs, task=task)
            else:
                enc_out, perm_idx = self.encode(xs, task='all')
        else:
            enc_out, perm_idx = self.encode(ys_sub1)
        ys = [ys[i] for i in perm_idx]

        observation = {}
        loss = Variable(enc_out[task]['xs'].new(1,).fill_(0.))

        # Compute XE loss for the forward decoder
        if self.fwd_weight > 0 and task == 'ys':
            loss_fwd, obs_fwd = self.dec_fwd(enc_out['ys']['xs'], enc_out['ys']['x_lens'], ys)
            loss += loss_fwd
            observation['loss.att'] = obs_fwd['loss_att']
            observation['loss.ctc'] = obs_fwd['loss_ctc']
            observation['loss.lm'] = obs_fwd['loss_lm']
            observation['acc.main'] = obs_fwd['acc']

        # Compute XE loss for the backward decoder
        if self.bwd_weight > 0:
            loss_bwd, obs_bwd = self.dec_bwd(enc_out['ys']['xs'], enc_out['ys']['x_lens'], ys)
            loss += loss_bwd
            observation['loss.att-bwd'] = obs_bwd['loss_att']
            observation['loss.ctc-bwd'] = obs_bwd['loss_ctc']
            observation['loss.lm-bwd'] = obs_bwd['loss_lm']
            observation['acc.bwd'] = obs_bwd['acc']
            # TODO(hirofumi): mtl_per_batch

        # for the 1st sub task
        if (self.sub1_weight > 0 and not self.mtl_per_batch) or (self.mtl_per_batch and task == 'ys_sub1'):
            if self.mtl_per_batch:
                loss_sub1, obs_sub1 = self.dec_fwd_sub1(
                    enc_out['ys_sub1']['xs'], enc_out['ys_sub1']['x_lens'], ys)
            else:
                ys_sub1 = [ys_sub1[i] for i in perm_idx]
                loss_sub1, obs_sub1 = self.dec_fwd_sub1(
                    enc_out['ys_sub1']['xs'], enc_out['ys_sub1']['x_lens'], ys_sub1)
            loss += loss_sub1
            observation['loss.att-sub1'] = obs_sub1['loss_att']
            observation['loss.ctc-sub1'] = obs_sub1['loss_ctc']
            observation['loss.lm-sub1'] = obs_sub1['loss_lm']
            observation['acc.sub1'] = obs_sub1['acc']

        # for the 2nd sub task
        if (self.sub2_weight > 0 and not self.mtl_per_batch) or (self.mtl_per_batch and task == 'ys_sub2'):
            if self.mtl_per_batch:
                loss_sub2, obs_sub2 = self.dec_fwd_sub2(
                    enc_out['ys_sub2']['xs'], enc_out['ys_sub2']['x_lens'], ys)
            else:
                ys_sub2 = [ys_sub2[i] for i in perm_idx]
                loss_sub2, obs_sub2 = self.dec_fwd_sub2(
                    enc_out['ys_sub2']['xs'], enc_out['ys_sub2']['x_lens'], ys_sub2)
            loss += loss_sub2
            observation['loss.att-sub2'] = obs_sub2['loss_att']
            observation['loss.ctc-sub2'] = obs_sub2['loss_ctc']
            observation['loss.lm-sub2'] = obs_sub2['loss_lm']
            observation['acc.sub2'] = obs_sub2['acc']
        # TODO(hirofumi): add sub_sub_task_weight

        return loss, observation

    def encode(self, xs, task='all'):
        """Encode acoustic or text features.

        Args:
            xs (list): A list of length `[B]`, which contains Variables of size `[T, input_dim]`
            task (str):
        Returns:
            enc_out (dict):
            perm_idx ():

        """
        # Sort by lenghts in the descending order
        perm_idx = sorted(list(six.moves.range(0, len(xs), 1)),
                          key=lambda i: len(xs[i]), reverse=True)
        xs = [xs[i] for i in perm_idx]
        # NOTE: must be descending order for pack_padded_sequence

        if self.input_type == 'speech':
            # Frame stacking
            if self.nstacks > 1:
                xs = [stack_frame(x, self.nstacks, self.nskips)for x in xs]

            # Splicing
            if self.nsplices > 1:
                xs = [splice(x, self.nsplices, self.nstacks) for x in xs]

            x_lens = [len(x) for x in xs]
            xs = [np2var(x, self.device_id).float() for x in xs]
            xs = pad_list(xs)

        elif self.input_type == 'text':
            x_lens = [len(x) for x in xs]
            xs = [np2var(np.fromiter(x, dtype=np.int64), self.device_id).long() for x in xs]
            xs = pad_list(xs, self.pad)
            xs = self.embed_in(xs)

        enc_out = self.enc(xs, x_lens, task)

        if self.main_weight < 1 and self.enc_type == 'cnn':
            enc_out['ys_sub1']['xs'] = enc_out['ys']['xs'].clone()
            enc_out['ys_sub2']['xs'] = enc_out['ys']['xs'].clone()
            enc_out['ys_sub1']['x_lens'] = copy.deepcopy(enc_out['ys']['x_lens'])
            enc_out['ys_sub2']['x_lens'] = copy.deepcopy(enc_out['ys']['x_lens'])

        # Bridge between the encoder and decoder
        if self.main_weight > 0 and (self.enc_type == 'cnn' or self.bridge_layer) and (task in ['all', 'ys']):
            enc_out['ys']['xs'] = self.bridge(enc_out['ys']['xs'])
        if self.sub1_weight > 0 and (self.enc_type == 'cnn' or self.bridge_layer) and (task in ['all', 'ys_sub1']):
            enc_out['ys_sub1']['xs'] = self.bridge_sub1(enc_out['ys_sub1']['xs'])
        if self.sub2_weight > 0 and (self.enc_type == 'cnn' or self.bridge_layer)and (task in ['all', 'ys_sub2']):
            enc_out['ys_sub2']['xs'] = self.bridge_sub2(enc_out['ys_sub2']['xs'])

        return enc_out, perm_idx

    def get_ctc_posteriors(self, xs, task='ys', temperature=1, topk=None):
        self.eval()
        with torch.no_grad():
            enc_out, perm_idx = self.encode(xs, task=task)
            dir = 'fwd' if self.fwd_weight >= self.bwd_weight else 'bwd'
            if task == 'ys_sub1':
                dir += '_sub1'
            elif task == 'ys_sub2':
                dir += '_sub2'

            if task == 'ys':
                assert self.ctc_weight > 0
            elif task == 'ys_sub1':
                assert self.ctc_weight_sub1 > 0
            elif task == 'ys_sub2':
                assert self.ctc_weight_sub2 > 0
            ctc_probs, indices_topk = getattr(self, 'dec_' + dir).ctc_posteriors(
                enc_out[task]['xs'], enc_out[task]['x_lens'], temperature, topk)
            return ctc_probs, indices_topk, enc_out[task]['x_lens']

    def decode(self, xs, decode_params, nbest=1, exclude_eos=False,
               idx2token=None, refs=None, ctc=False, task='ys'):
        """Decoding in the inference stage.

        Args:
            xs (list): A list of length `[B]`, which contains arrays of size `[T, input_dim]`
            decode_params (dict):
                beam_width (int): the size of beam
                min_len_ratio (float):
                max_len_ratio (float):
                len_penalty (float): length penalty
                cov_penalty (float): coverage penalty
                cov_threshold (float): threshold for coverage penalty
                rnnlm_weight (float): the weight of RNNLM score
                resolving_unk (bool): not used (to make compatible)
                fwd_bwd_attention (bool):
            nbest (int):
            exclude_eos (bool): exclude <eos> from best_hyps
            idx2token (): converter from index to token
            refs (list): gold transcriptions to compute log likelihood
            ctc (bool):
            task (str): ys or ys_sub1 or ys_sub2
        Returns:
            best_hyps (list): A list of length `[B]`, which contains arrays of size `[L]`
            aws (list): A list of length `[B]`, which contains arrays of size `[L, T]`
            perm_idx (list): A list of length `[B]`

        """
        self.eval()
        with torch.no_grad():
            enc_out, perm_idx = self.encode(xs, task=task)
            dir = 'fwd' if self.fwd_weight >= self.bwd_weight else 'bwd'
            if task == 'ys_sub1':
                dir += '_sub1'
            elif task == 'ys_sub2':
                dir += '_sub2'

            if self.ctc_weight == 1 or (self.ctc_weight > 0 and ctc):
                # Set RNNLM
                if decode_params['rnnlm_weight'] > 0:
                    assert hasattr(self, 'rnnlm_' + dir)
                    rnnlm = getattr(self, 'rnnlm_' + dir)
                else:
                    rnnlm = None

                best_hyps = getattr(self, 'dec_' + dir).decode_ctc(
                    enc_out[task]['xs'], enc_out[task]['x_lens'],
                    decode_params['beam_width'], rnnlm)
                return best_hyps, None, perm_idx
            else:
                if decode_params['beam_width'] == 1 and not decode_params['fwd_bwd_attention']:
                    best_hyps, aws = getattr(self, 'dec_' + dir).greedy(
                        enc_out[task]['xs'], enc_out[task]['x_lens'],
                        decode_params['max_len_ratio'], exclude_eos)
                else:
                    if decode_params['fwd_bwd_attention']:
                        rnnlm_fwd = None
                        nbest_hyps_fwd, aws_fwd, scores_fwd = self.dec_fwd.beam_search(
                            enc_out[task]['xs'], enc_out[task]['x_lens'],
                            decode_params, rnnlm_fwd,
                            decode_params['beam_width'], False, idx2token, refs)

                        rnnlm_bwd = None
                        nbest_hyps_bwd, aws_bwd, scores_bwd = self.dec_bwd.beam_search(
                            enc_out[task]['xs'], enc_out[task]['x_lens'],
                            decode_params, rnnlm_bwd,
                            decode_params['beam_width'], False, idx2token, refs)
                        best_hyps = fwd_bwd_attention(nbest_hyps_fwd, aws_fwd, scores_fwd,
                                                      nbest_hyps_bwd, aws_bwd, scores_bwd,
                                                      idx2token, refs)
                        aws = None
                    else:
                        # Set RNNLM
                        if decode_params['rnnlm_weight'] > 0:
                            assert hasattr(self, 'rnnlm_' + dir)
                            rnnlm = getattr(self, 'rnnlm_' + dir)
                        else:
                            rnnlm = None
                        nbest_hyps, aws, scores = getattr(self, 'dec_' + dir).beam_search(
                            enc_out[task]['xs'], enc_out[task]['x_lens'],
                            decode_params, rnnlm,
                            nbest, exclude_eos, idx2token, refs)

                        if nbest == 1:
                            best_hyps = [hyp[0] for hyp in nbest_hyps]
                            aws = [aw[0] for aw in aws]
                        else:
                            return nbest_hyps, aws, scores, perm_idx
                        # NOTE: nbest >= 2 is used for MWER training only

                return best_hyps, aws, perm_idx


def fwd_bwd_attention(nbest_hyps_fwd, aws_fwd, scores_fwd,
                      nbest_hyps_bwd, aws_bwd, scores_bwd,
                      idx2token=None, refs=None):
    """Forward-backward joint decoding.
    Args:
        nbest_hyps_fwd (list): A list of length `[B]`, which contains list of n hypotheses
        aws_fwd (list): A list of length `[B]`, which contains arrays of size `[L, T]`
        scores_fwd (list):
        nbest_hyps_bwd (list):
        aws_bwd (list):
        scores_bwd (list):
        idx2token (): converter from index to token
        refs ():
    Returns:

    """
    logger = logging.getLogger("decoding")
    batch_size = len(nbest_hyps_fwd)
    nbest = len(nbest_hyps_fwd[0])
    eos = 2

    best_hyps = []
    for b in range(batch_size):
        merged = []
        for n in range(nbest):
            # forward
            if len(nbest_hyps_fwd[b][n]) > 1:
                if nbest_hyps_fwd[b][n][-1] == eos:
                    merged.append({'hyp': nbest_hyps_fwd[b][n][:-1],
                                   'score': scores_fwd[b][n][-2]})
                   # NOTE: remove eos probability
                else:
                    merged.append({'hyp': nbest_hyps_fwd[b][n],
                                   'score': scores_fwd[b][n][-1]})
            else:
                # <eos> only
                logger.info(nbest_hyps_fwd[b][n])

            # backward
            if len(nbest_hyps_bwd[b][n]) > 1:
                if nbest_hyps_bwd[b][n][0] == eos:
                    merged.append({'hyp': nbest_hyps_bwd[b][n][1:],
                                   'score': scores_bwd[b][n][1]})
                   # NOTE: remove eos probability
                else:
                    merged.append({'hyp': nbest_hyps_bwd[b][n],
                                   'score': scores_bwd[b][n][0]})
            else:
                # <eos> only
                logger.info(nbest_hyps_bwd[b][n])

        for n_f in range(nbest):
            for n_b in range(nbest):
                for i_f in range(len(aws_fwd[b][n_f]) - 1):
                    for i_b in range(len(aws_bwd[b][n_b]) - 1):
                        t_prev = aws_bwd[b][n_b][i_b + 1].argmax(-1).item()
                        t_curr = aws_fwd[b][n_f][i_f].argmax(-1).item()
                        t_next = aws_bwd[b][n_b][i_b - 1].argmax(-1).item()

                        # the same token at the same time
                        if t_curr >= t_prev and t_curr <= t_next and nbest_hyps_fwd[b][n_f][i_f] == nbest_hyps_bwd[b][n_b][i_b]:
                            new_hyp = nbest_hyps_fwd[b][n_f][:i_f + 1].tolist() + \
                                nbest_hyps_bwd[b][n_b][i_b + 1:].tolist()
                            score_curr_fwd = scores_fwd[b][n_f][i_f] - scores_fwd[b][n_f][i_f - 1]
                            score_curr_bwd = scores_bwd[b][n_b][i_b] - scores_bwd[b][n_b][i_b + 1]
                            score_curr = max(score_curr_fwd, score_curr_bwd)
                            new_score = scores_fwd[b][n_f][i_f - 1] + scores_bwd[b][n_b][i_b + 1] + score_curr
                            merged.append({'hyp': new_hyp, 'score': new_score})

                            logger.info('time matching')
                            if idx2token is not None:
                                if refs is not None:
                                    logger.info('Ref: %s' % refs[b].lower())
                                logger.info('hyp (fwd): %s' % idx2token(nbest_hyps_fwd[b][n_f]))
                                logger.info('hyp (bwd): %s' % idx2token(nbest_hyps_bwd[b][n_b]))
                                logger.info('hyp (fwd-bwd): %s' % idx2token(new_hyp))
                            logger.info('log prob (fwd): %.3f' % scores_fwd[b][n_f][-1])
                            logger.info('log prob (bwd): %.3f' % scores_bwd[b][n_b][0])
                            logger.info('log prob (fwd-bwd): %.3f' % new_score)

        merged = sorted(merged, key=lambda x: x['score'], reverse=True)
        best_hyps.append(merged[0]['hyp'])

    return best_hyps
