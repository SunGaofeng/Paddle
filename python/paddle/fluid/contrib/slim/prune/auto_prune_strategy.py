# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .prune_strategy import PruneStrategy
import re
import logging
import functools
import copy

__all__ = ['AutoPruneStrategy']

logging.basicConfig(format='%(asctime)s-%(levelname)s: %(message)s')
_logger = logging.getLogger(__name__)
_logger.setLevel(logging.INFO)


class AutoPruneStrategy(PruneStrategy):
    """
    Automatic pruning strategy.
    """

    def __init__(self,
                 pruner=None,
                 controller=None,
                 start_epoch=0,
                 end_epoch=10,
                 min_ratio=0.5,
                 max_ratio=0.7,
                 metric_name='top1_acc',
                 pruned_params='conv.*_weights',
                 retrain_epoch=0):
        """
        Args:
            pruner(slim.Pruner): The pruner used to prune the parameters. Default: None.
            controller(searcher.Controller): The searching controller. Default: None.
            start_epoch(int): The 'on_epoch_begin' function will be called in start_epoch. Default: 0
            end_epoch(int): The 'on_epoch_end' function will be called in end_epoch. Default: 0
            min_ratio(float): The maximum pruned ratio. Default: 0.7
            max_ratio(float): The minimum pruned ratio. Default: 0.5
            metric_name(str): The metric used to evaluate the model.
                         It should be one of keys in out_nodes of graph wrapper. Default: 'top1_acc'
            pruned_params(str): The pattern str to match the parameter names to be pruned. Default: 'conv.*_weights'
            retrain_epoch(int): The training epochs in each seaching step. Default: 0
        """
        super(AutoPruneStrategy, self).__init__(pruner, start_epoch, end_epoch,
                                                0.0, metric_name, pruned_params)
        self._max_ratio = max_ratio
        self._min_ratio = min_ratio
        self._controller = controller
        self._metric_name = metric_name
        self._pruned_param_names = []
        self._retrain_epoch = 0

        self._current_tokens = None

    def on_compression_begin(self, context):
        """
        Prepare some information for searching strategy.
        step 1: Find all the parameters to be pruned.
        step 2: Get initial tokens and setup controller.
        """
        pruned_params = []
        for param in context.eval_graph.all_parameters():
            if re.match(self.pruned_params, param.name()):
                self._pruned_param_names.append(param.name())

        self._current_tokens = self._get_init_tokens(context)
        self._range_table = copy.deepcopy(self._current_tokens)

        constrain_func = functools.partial(
            self._constrain_func, context=context)

        self._controller.reset(self._range_table, self._current_tokens,
                               constrain_func)

    def _constrain_func(self, tokens, context=None):
        """Check whether the tokens meet constraint."""
        ori_flops = context.eval_graph.flops()
        ratios = self._tokens_to_ratios(tokens)
        params = self._pruned_param_names
        param_shape_backup = {}
        self._prune_parameters(
            context.eval_graph,
            context.scope,
            params,
            ratios,
            context.place,
            only_graph=True,
            param_shape_backup=param_shape_backup)
        context.eval_graph.update_groups_of_conv()
        flops = context.eval_graph.flops()
        for param in param_shape_backup.keys():
            context.eval_graph.var(param).set_shape(param_shape_backup[param])
        flops_ratio = (1 - float(flops) / ori_flops)
        if flops_ratio >= self._min_ratio and flops_ratio <= self._max_ratio:
            return True
        else:
            return False

    def _get_init_tokens(self, context):
        """Get initial tokens.
        """
        ratios = self._get_uniform_ratios(context)
        return self._ratios_to_tokens(ratios)

    def _ratios_to_tokens(self, ratios):
        """Convert pruned ratios to tokens.
        """
        return [int(ratio / 0.01) for ratio in ratios]

    def _tokens_to_ratios(self, tokens):
        """Convert tokens to pruned ratios.
        """
        return [token * 0.01 for token in tokens]

    def _get_uniform_ratios(self, context):
        """
        Search a group of uniform ratios.
        """
        min_ratio = 0.
        max_ratio = 1.
        target = (self._min_ratio + self._max_ratio) / 2
        flops = context.eval_graph.flops()
        model_size = context.eval_graph.numel_params()
        ratios = None
        while min_ratio < max_ratio:
            ratio = (max_ratio + min_ratio) / 2
            ratios = [ratio] * len(self._pruned_param_names)
            param_shape_backup = {}
            self._prune_parameters(
                context.eval_graph,
                context.scope,
                self._pruned_param_names,
                ratios,
                context.place,
                only_graph=True,
                param_shape_backup=param_shape_backup)

            pruned_flops = 1 - (float(context.eval_graph.flops()) / flops)
            pruned_size = 1 - (float(context.eval_graph.numel_params()) /
                               model_size)
            for param in param_shape_backup.keys():
                context.eval_graph.var(param).set_shape(param_shape_backup[
                    param])

            if abs(pruned_flops - target) < 1e-2:
                break
            if pruned_flops > target:
                max_ratio = ratio
            else:
                min_ratio = ratio
        _logger.info('Get ratios: {}'.format([round(r, 2) for r in ratios]))
        return ratios

    def on_epoch_begin(self, context):
        """
        step 1: Get a new tokens from controller.
        step 2: Pruning eval_graph and optimize_program by tokens
        """
        if context.epoch_id >= self.start_epoch and context.epoch_id <= self.end_epoch and (
                self._retrain_epoch == 0 or
            (context.epoch_id - self.start_epoch) % self._retrain_epoch == 0):
            self._current_tokens = self._controller.next_tokens()
            params = self._pruned_param_names
            ratios = self._tokens_to_ratios(self._current_tokens)

            self._param_shape_backup = {}
            self._param_backup = {}
            self._prune_parameters(
                context.optimize_graph,
                context.scope,
                params,
                ratios,
                context.place,
                param_backup=self._param_backup,
                param_shape_backup=self._param_shape_backup)
            self._prune_graph(context.eval_graph, context.optimize_graph)
            context.optimize_graph.update_groups_of_conv()
            context.eval_graph.update_groups_of_conv()
            context.optimize_graph.compile(
                mem_opt=True)  # to update the compiled program
            context.skip_training = (self._retrain_epoch == 0)

    def on_epoch_end(self, context):
        """
        step 1: Get reward of current tokens and update controller.
        step 2: Restore eval_graph and optimize_graph
        """
        if context.epoch_id >= self.start_epoch and context.epoch_id < self.end_epoch and (
                self._retrain_epoch == 0 or
            (context.epoch_id - self.start_epoch) % self._retrain_epoch == 0):
            reward = context.eval_results[self._metric_name][-1]
            self._controller.update(self._current_tokens, reward)

            # restore pruned parameters
            for param_name in self._param_backup.keys():
                param_t = context.scope.find_var(param_name).get_tensor()
                param_t.set(self._param_backup[param_name], context.place)
            self._param_backup = {}
            # restore shape of parameters
            for param in self._param_shape_backup.keys():
                context.optimize_graph.var(param).set_shape(
                    self._param_shape_backup[param])
            self._param_shape_backup = {}
            self._prune_graph(context.eval_graph, context.optimize_graph)

            context.optimize_graph.update_groups_of_conv()
            context.eval_graph.update_groups_of_conv()
            context.optimize_graph.compile(
                mem_opt=True)  # to update the compiled program

        elif context.epoch_id == self.end_epoch:  # restore graph for final training
            # restore pruned parameters
            for param_name in self._param_backup.keys():
                param_t = context.scope.find_var(param_name).get_tensor()
                param_t.set(self.param_backup[param_name], context.place)
            # restore shape of parameters
            for param in self._param_shape_backup.keys():
                context.eval_graph.var(param).set_shape(
                    self._param_shape_backup[param])
                context.optimize_graph.var(param).set_shape(
                    self._param_shape_backup[param])

            context.optimize_graph.update_groups_of_conv()
            context.eval_graph.update_groups_of_conv()

            params, ratios = self._get_prune_ratios(
                self._controller._best_tokens)
            self._prune_parameters(context.optimize_graph, context.scope,
                                   params, ratios, context.place)

            self._prune_graph(context.eval_graph, context.optimize_graph)
            context.optimize_graph.update_groups_of_conv()
            context.eval_graph.update_groups_of_conv()
            context.optimize_graph.compile(
                mem_opt=True)  # to update the compiled program

            context.skip_training = False
