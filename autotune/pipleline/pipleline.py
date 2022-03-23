# License: MIT

import os
import abc
import numpy as np
import sys
import time
import traceback
import math
from typing import List
from collections import OrderedDict
from tqdm import tqdm
from openbox.utils.util_funcs import check_random_state
from openbox.utils.logging_utils import get_logger
from autotune.utils.history_container import HistoryContainer, MOHistoryContainer, MultiStartHistoryContainer
from openbox.utils.constants import MAXINT, SUCCESS
from openbox.utils.samplers import SobolSampler, LatinHypercubeSampler
from openbox.utils.multi_objective import get_chebyshev_scalarization, NondominatedPartitioning
from openbox.utils.config_space.util import convert_configurations_to_array
from openbox.core.base import Observation
from autotune.pipleline.base import BOBase
from openbox.utils.constants import MAXINT, SUCCESS, FAILED, TIMEOUT
from openbox.utils.limit import time_limit, TimeoutException
from openbox.utils.util_funcs import get_result
from autotune.selector.selector import SHAPSelector, fANOVASelector, GiniSelector, AblationSelector, LASSOSelector
from autotune.optimizer.surrogate.core import build_surrogate, surrogate_switch
from autotune.optimizer.core import build_acq_func, build_optimizer
import pdb

class PipleLine(BOBase):
    """
    Basic Advisor Class, which adopts a policy to sample a configuration.
    """

    def __init__(self, objective_function: callable,
                 config_space,
                 sample_strategy: str = 'bo',
                 max_runs=200,
                 runtime_limit=None,
                 time_limit_per_trial=180,
                 surrogate_type='auto',
                 acq_type='auto',
                 acq_optimizer_type='auto',
                 initial_runs=3,
                 init_strategy='random_explore_first',
                 initial_configurations=None,
                 ref_point=None,
                 history_bo_data: List[OrderedDict] = None,
                 logging_dir='logs',
                 task_id='default_task_id',
                 random_state=None,
                 num_objs=1,
                 num_constraints=0,
                 initial_trials=3,
                 rand_prob=0.1,
                 optimization_strategy='bo',
                 selector_type='shap',
                 incremental=None,
                 incremental_step=4,
                 incremental_num=1,
                 num_hps=5):


        self.FAILED_PERF = [MAXINT] * num_objs
        super().__init__(objective_function, config_space, task_id=task_id, output_dir=logging_dir,
                         random_state=random_state, initial_runs=initial_runs, max_runs=max_runs,
                         runtime_limit=runtime_limit, sample_strategy=sample_strategy,
                         time_limit_per_trial=time_limit_per_trial, history_bo_data=history_bo_data)

        # Create output (logging) directory.
        # Init logging module.
        # Random seed generator.
        self.num_objs = num_objs
        self.num_constraints = num_constraints
        self.init_strategy = init_strategy
        self.output_dir = logging_dir
        self.task_id = task_id
        self.rng = check_random_state(random_state)
        self.logger = get_logger(self.__class__.__name__)

        # Basic components in Advisor.
        self.rand_prob = rand_prob
        self.optimization_strategy = optimization_strategy

        # Init the basic ingredients in Bayesian optimization.
        self.history_bo_data = history_bo_data
        self.surrogate_type = surrogate_type
        self.constraint_surrogate_type = None
        self.acq_type = acq_type
        self.acq_optimizer_type = acq_optimizer_type
        self.init_num = initial_trials
        self.config_space = config_space
        self.config_space_seed = self.rng.randint(MAXINT)
        self.config_space.seed(self.config_space_seed)
        self.ref_point = ref_point

        self.config_space_all = config_space

        # init history container
        self.history_container = HistoryContainer(task_id, self.num_constraints, config_space=self.config_space)

        # initial design
        if initial_configurations is not None and len(initial_configurations) > 0:
            self.initial_configurations = initial_configurations
            self.init_num = len(initial_configurations)
        else:
            self.initial_configurations = self.create_initial_design(self.init_strategy)
            self.init_num = len(self.initial_configurations)

        # knobs selector
        self.selector_type = selector_type
        self.incremental = incremental              # None, increase, decrease
        self.incremental_step = incremental_step    # how often increment the number of knobs
        self.incremental_num = incremental_num      # how many knobs to increment each time
        self.num_hps = num_hps                      # initial number of knobs before incremental tuning
        self.num_hps_max = len(self.config_space_all.get_hyperparameters())
        self.init_selector()

        self.surrogate_model = None
        self.constraint_models = None
        self.acquisition_function = None
        self.optimizer = None
        self.auto_alter_model = False
        self.algo_auto_selection()
        self.check_setup()
        self.setup_bo_basics()


    def init_selector(self):
        if self.selector_type == 'shap':
            self.selector = SHAPSelector()
        elif self.selector_type == 'fanova':
            self.selector = fANOVASelector()
        elif self.selector_type == 'gini':
            self.selector = GiniSelector()
        elif self.selector_type == 'ablation':
            self.selector = AblationSelector()
        elif self.selector_type == 'lasso':
            self.selector = LASSOSelector()

    def run(self):
        for _ in tqdm(range(self.iteration_id, self.max_iterations)):
            if self.budget_left < 0:
                self.logger.info('Time %f elapsed!' % self.runtime_limit)
                break
            start_time = time.time()
            self.iterate(budget_left=self.budget_left)
            runtime = time.time() - start_time
            self.budget_left -= runtime
        return self.get_history()


    def alter_config_space(self, config_space):
        self.config_space = config_space
        self.history_container.alter_configuration_space(config_space)
        num_config_evaluated = len(self.history_container.configurations)
        if num_config_evaluated < self.init_num:
            add_configs = self.create_initial_design()[num_config_evaluated:]
            self.initial_configurations = self.initial_configurations[:num_config_evaluated] + add_configs
        self.setup_bo_basics()



    def knob_selection(self):
        if self.incremental \
                and self.iteration_id > self.init_num \
                and (self.iteration_id - self.init_num) % self.incremental_step == 0:
            if self.incremental == 'increase':
                self.num_hps = min(self.num_hps + 1, self.num_hps_max)
            elif self.incremental == 'decrease':
                self.num_hps = max(self.num_hps - 1, 1)
            new_config_space = self.selector.knob_selection(self.config_space_all, self.history_container, self.num_hps)
            self.alter_config_space(new_config_space)
        else:
            if self.iteration_id == self.init_num:
                new_config_space = self.selector.knob_selection(
                    self.config_space_all, self.history_container, self.num_hps)
                self.alter_config_space(new_config_space)

    def iterate(self, budget_left=None):
        self.knob_selection()
        # if have enough data, get_suggorate
        num_config_evaluated = len(self.history_container.configurations)
        if num_config_evaluated >= self.init_num:
            self.get_surrogate()

        # get configuration suggestion
        config = self.get_suggestion()
        _, trial_state, constraints, objs = self.evaluate(config, budget_left)

        return config, trial_state, constraints, objs




    def algo_auto_selection(self):
        from ConfigSpace import UniformFloatHyperparameter, UniformIntegerHyperparameter, \
            CategoricalHyperparameter, OrdinalHyperparameter
        # analyze config space
        cont_types = (UniformFloatHyperparameter, UniformIntegerHyperparameter)
        cat_types = (CategoricalHyperparameter, OrdinalHyperparameter)
        n_cont_hp, n_cat_hp, n_other_hp = 0, 0, 0
        for hp in self.config_space.get_hyperparameters():
            if isinstance(hp, cont_types):
                n_cont_hp += 1
            elif isinstance(hp, cat_types):
                n_cat_hp += 1
            else:
                n_other_hp += 1
        n_total_hp = n_cont_hp + n_cat_hp + n_other_hp

        info_str = ''

        if self.surrogate_type == 'auto':
            self.auto_alter_model = True
            if n_total_hp >= 100:
                self.optimization_strategy = 'random'
                self.surrogate_type = 'prf'  # for setup procedure
            elif n_total_hp >= 10:
                self.surrogate_type = 'prf'
            elif n_cat_hp > n_cont_hp:
                self.surrogate_type = 'prf'
            else:
                self.surrogate_type = 'gp'
            info_str += ' surrogate_type: %s.' % self.surrogate_type

        if self.acq_type == 'auto':
            if self.num_objs == 1:  # single objective
                if self.num_constraints == 0:
                    self.acq_type = 'ei'
                else:   # with constraints
                    self.acq_type = 'eic'
            elif self.num_objs <= 4:    # multi objective (<=4)
                if self.num_constraints == 0:
                    self.acq_type = 'ehvi'
                else:   # with constraints
                    self.acq_type = 'ehvic'
            else:   # multi objective (>4)
                if self.num_constraints == 0:
                    self.acq_type = 'mesmo'
                else:   # with constraints
                    self.acq_type = 'mesmoc'
                self.surrogate_type = 'gp_rbf'
                info_str = ' surrogate_type: %s.' % self.surrogate_type
            info_str += ' acq_type: %s.' % self.acq_type

        if self.acq_optimizer_type == 'auto':
            if n_cat_hp + n_other_hp == 0:  # todo: support constant hp in scipy optimizer
                self.acq_optimizer_type = 'random_scipy'
            else:
                self.acq_optimizer_type = 'local_random'
            info_str += ' acq_optimizer_type: %s.' % self.acq_optimizer_type

        if info_str != '':
            info_str = '=== [BO auto selection] ===' + info_str
            self.logger.info(info_str)

    def alter_model(self, history_container):
        if not self.auto_alter_model:
            return

        num_config_evaluated = len(history_container.configurations)
        num_config_successful = len(history_container.successful_perfs)

        if num_config_evaluated == 300:
            if self.surrogate_type == 'gp':
                self.surrogate_type = 'prf'
                self.logger.info('n_observations=300, change surrogate model from GP to PRF!')
                if self.acq_optimizer_type == 'random_scipy':
                    self.acq_optimizer_type = 'local_random'
                    self.logger.info('n_observations=300, change acq optimizer from random_scipy to local_random!')
                self.setup_bo_basics()

    def check_setup(self):
        """
        Check optimization_strategy, num_objs, num_constraints, acq_type, surrogate_type.
        Returns
        -------
        None
        """
        assert self.optimization_strategy in ['bo', 'random']
        assert isinstance(self.num_objs, int) and self.num_objs >= 1
        assert isinstance(self.num_constraints, int) and self.num_constraints >= 0

        # single objective
        if self.num_objs == 1:
            if self.num_constraints == 0:
                assert self.acq_type in ['ei', 'eips', 'logei', 'pi', 'lcb', 'lpei', ]
            else:  # with constraints
                assert self.acq_type in ['eic', ]
                if self.constraint_surrogate_type is None:
                    self.constraint_surrogate_type = 'gp'

        # multi-objective
        else:
            if self.num_constraints == 0:
                assert self.acq_type in ['ehvi', 'mesmo', 'usemo', 'parego']
                if self.acq_type == 'mesmo' and self.surrogate_type != 'gp_rbf':
                    self.surrogate_type = 'gp_rbf'
                    self.logger.warning('Surrogate model has changed to Gaussian Process with RBF kernel '
                                        'since MESMO is used. Surrogate_type should be set to \'gp_rbf\'.')
            else:  # with constraints
                assert self.acq_type in ['ehvic', 'mesmoc', 'mesmoc2']
                if self.constraint_surrogate_type is None:
                    if self.acq_type == 'mesmoc':
                        self.constraint_surrogate_type = 'gp_rbf'
                    else:
                        self.constraint_surrogate_type = 'gp'
                if self.acq_type == 'mesmoc' and self.surrogate_type != 'gp_rbf':
                    self.surrogate_type = 'gp_rbf'
                    self.logger.warning('Surrogate model has changed to Gaussian Process with RBF kernel '
                                        'since MESMOC is used. Surrogate_type should be set to \'gp_rbf\'.')
                if self.acq_type == 'mesmoc' and self.constraint_surrogate_type != 'gp_rbf':
                    self.surrogate_type = 'gp_rbf'
                    self.logger.warning('Constraint surrogate model has changed to Gaussian Process with RBF kernel '
                                        'since MESMOC is used. Surrogate_type should be set to \'gp_rbf\'.')

            # Check reference point is provided for EHVI methods
            if 'ehvi' in self.acq_type and self.ref_point is None:
                raise ValueError('Must provide reference point to use EHVI method!')

    def setup_bo_basics(self):
        """
        Prepare the basic BO components.
        Returns
        -------
        An optimizer object.
        """

        self.surrogate_model = build_surrogate(func_str=self.surrogate_type,
                                                   config_space=self.config_space,
                                                   rng=self.rng,
                                                   history_hpo_data=self.history_bo_data)


        if self.acq_type in ['mesmo', 'mesmoc', 'mesmoc2', 'usemo']:
            self.acquisition_function = build_acq_func(func_str=self.acq_type,
                                                       model=self.surrogate_model,
                                                       constraint_models=self.constraint_models,
                                                       config_space=self.config_space)
        else:
            self.acquisition_function = build_acq_func(func_str=self.acq_type,
                                                       model=self.surrogate_model,
                                                       constraint_models=self.constraint_models,
                                                       ref_point=self.ref_point)
        if self.acq_type == 'usemo':
            self.acq_optimizer_type = 'usemo_optimizer'
        self.optimizer = build_optimizer(func_str=self.acq_optimizer_type,
                                         acq_func=self.acquisition_function,
                                         config_space=self.config_space,
                                         rng=self.rng)

    def create_initial_design(self, init_strategy='default'):
        """
        Create several configurations as initial design.
        Parameters
        ----------
        init_strategy: str

        Returns
        -------
        Initial configurations.
        """
        default_config = self.config_space.get_default_configuration()
        num_random_config = self.init_num - 1
        if init_strategy == 'random':
            initial_configs = self.sample_random_configs(self.init_num)
            return initial_configs
        elif init_strategy == 'default':
            initial_configs = [default_config] + self.sample_random_configs(num_random_config)
            return initial_configs
        elif init_strategy == 'random_explore_first':
            candidate_configs = self.sample_random_configs(100)
            return self.max_min_distance(default_config, candidate_configs, num_random_config)
        elif init_strategy == 'sobol':
            sobol = SobolSampler(self.config_space, num_random_config, random_state=self.rng)
            initial_configs = [default_config] + sobol.generate(return_config=True)
            return initial_configs
        elif init_strategy == 'latin_hypercube':
            lhs = LatinHypercubeSampler(self.config_space, num_random_config, criterion='maximin')
            initial_configs = [default_config] + lhs.generate(return_config=True)
            return initial_configs
        else:
            raise ValueError('Unknown initial design strategy: %s.' % init_strategy)

    def max_min_distance(self, default_config, src_configs, num):
        min_dis = list()
        initial_configs = list()
        initial_configs.append(default_config)

        for config in src_configs:
            dis = np.linalg.norm(config.get_array() - default_config.get_array())
            min_dis.append(dis)
        min_dis = np.array(min_dis)

        for i in range(num):
            furthest_config = src_configs[np.argmax(min_dis)]
            initial_configs.append(furthest_config)
            min_dis[np.argmax(min_dis)] = -1

            for j in range(len(src_configs)):
                if src_configs[j] in initial_configs:
                    continue
                updated_dis = np.linalg.norm(src_configs[j].get_array() - furthest_config.get_array())
                min_dis[j] = min(updated_dis, min_dis[j])

        return initial_configs


    def get_surrogate(self, history_container=None):

        if history_container is None:
            history_container = self.history_container

        X = convert_configurations_to_array(history_container.configurations)
        Y = history_container.get_transformed_perfs()
        cY = history_container.get_transformed_constraint_perfs()

        if self.optimization_strategy == 'bo':
            self.surrogate_model.train(X, Y)


    def get_suggestion(self, history_container=None, return_list=False):
        """
        Generate a configuration (suggestion) for this query.
        Returns
        -------
        A configuration.
        """
        if history_container is None:
            history_container = self.history_container

        self.alter_model(history_container)

        num_config_evaluated = len(history_container.configurations)
        num_config_successful = len(history_container.successful_perfs)

        if num_config_evaluated < self.init_num:
            return self.initial_configurations[num_config_evaluated]

        if self.optimization_strategy == 'random':
            return self.sample_random_configs(1, history_container)[0]

        if (not return_list) and self.rng.random() < self.rand_prob:
            self.logger.info('Sample random config. rand_prob=%f.' % self.rand_prob)
            return self.sample_random_configs(1, history_container)[0]

        if self.optimization_strategy == 'bo':
            # update acquisition function
            if self.num_objs == 1:
                incumbent_value = history_container.get_incumbents()[0][1]
                self.acquisition_function.update(model=self.surrogate_model,
                                                 constraint_models=self.constraint_models,
                                                 eta=incumbent_value,
                                                 num_data=num_config_evaluated)


            # optimize acquisition function
            challengers = self.optimizer.maximize(runhistory=history_container,
                                                  num_points=5000)
            if return_list:
                # Caution: return_list doesn't contain random configs sampled according to rand_prob
                return challengers.challengers

            for config in challengers.challengers:
                if config not in history_container.configurations:
                    return config
            self.logger.warning('Cannot get non duplicate configuration from BO candidates (len=%d). '
                                'Sample random config.' % (len(challengers.challengers), ))
            return self.sample_random_configs(1, history_container)[0]
        else:
            raise ValueError('Unknown optimization strategy: %s.' % self.optimization_strategy)

    def update_observation(self, observation: Observation):
        """
        Update the current observations.
        Parameters
        ----------
        observation

        Returns
        -------

        """
        return self.history_container.update_observation(observation)

    def sample_random_configs(self, num_configs=1, history_container=None, excluded_configs=None):
        """
        Sample a batch of random configurations.
        Parameters
        ----------
        num_configs

        history_container

        Returns
        -------

        """
        if history_container is None:
            history_container = self.history_container
        if excluded_configs is None:
            excluded_configs = set()

        configs = list()
        sample_cnt = 0
        max_sample_cnt = 1000
        while len(configs) < num_configs:
            config = self.config_space.sample_configuration()
            sample_cnt += 1
            if config not in (history_container.configurations + configs) and config not in excluded_configs:
                configs.append(config)
                sample_cnt = 0
                continue
            if sample_cnt >= max_sample_cnt:
                self.logger.warning('Cannot sample non duplicate configuration after %d iterations.' % max_sample_cnt)
                configs.append(config)
                sample_cnt = 0
        return configs

    def get_history(self):
        return self.history_container

    def save_history(self, dir_path: str = None, file_name: str = None):
        """
        Save history to a json file.
        """
        if dir_path is None:
            dir_path = os.path.join(self.output_dir, 'bo_history')
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)
        if file_name is None:
            file_name = 'bo_history_%s.json' % self.task_id
        return self.history_container.save_json(os.path.join(dir_path, file_name))

    def load_history_from_json(self, fn=None):
        """
        Load history from a json file.
        """
        if fn is None:
            fn = os.path.join(self.output_dir, 'bo_history', 'bo_history_%s.json' % self.task_id)
        return self.history_container.load_history_from_json(self.config_space, fn)

    def get_suggestions(self):
        raise NotImplementedError


    def evaluate(self, config, budget_left=None):
        trial_state = SUCCESS
        _budget_left = int(1e10) if budget_left is None else budget_left
        _time_limit_per_trial = math.ceil(min(self.time_limit_per_trial, _budget_left))

        # only evaluate non duplicate configuration
        if config not in self.history_container.configurations:
            start_time = time.time()
            try:
                # evaluate configuration on objective_function within time_limit_per_trial
                args, kwargs = (config,), dict()
                timeout_status, _result = time_limit(self.objective_function,
                                                     _time_limit_per_trial,
                                                     args=args, kwargs=kwargs)
                if timeout_status:
                    raise TimeoutException(
                        'Timeout: time limit for this evaluation is %.1fs' % _time_limit_per_trial)
                else:
                    # parse result
                    objs, constraints = get_result(_result)
            except Exception as e:
                # parse result of failed trial
                if isinstance(e, TimeoutException):
                    self.logger.warning(str(e))
                    trial_state = TIMEOUT
                else:
                    self.logger.warning('Exception when calling objective function: %s' % str(e))
                    trial_state = FAILED
                objs = self.FAILED_PERF
                constraints = None

            elapsed_time = time.time() - start_time
            # update observation to advisor
            observation = Observation(
                config=config, objs=objs, constraints=constraints,
                trial_state=trial_state, elapsed_time=elapsed_time,
            )
            if _time_limit_per_trial != self.time_limit_per_trial and trial_state == TIMEOUT:
                # Timeout in the last iteration.
                pass
            else:
                self.update_observation(observation)
        else:
            self.logger.info('This configuration has been evaluated! Skip it: %s' % config)
            history = self.get_history()
            config_idx = history.configurations.index(config)
            trial_state = history.trial_states[config_idx]
            objs = history.perfs[config_idx]
            constraints = history.constraint_perfs[config_idx] if self.num_constraints > 0 else None
            if self.num_objs == 1:
                objs = (objs,)

        self.iteration_id += 1
        # Logging.
        if self.num_constraints > 0:
            self.logger.info('Iteration %d, objective value: %s. constraints: %s.'
                             % (self.iteration_id, objs, constraints))
        else:
            self.logger.info('Iteration %d, objective value: %s.' % (self.iteration_id, objs))

        # Visualization.
        # for idx, obj in enumerate(objs):
        #     if obj < self.FAILED_PERF[idx]:
        #         self.writer.add_scalar('data/objective-%d' % (idx + 1), obj, self.iteration_id)
        return config, trial_state, constraints, objs



