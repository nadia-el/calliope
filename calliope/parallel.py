"""
Copyright (C) 2013 Stefan Pfenninger.
Licensed under the Apache 2.0 License (see LICENSE file).

parallel.py
~~~~~~~~~~~

Defines the Parallelizer class which manages the creation of arbitrary
numbers of scripted runs, either locally or to be deployed to a cluster.

"""

from __future__ import print_function
from __future__ import division

import copy
import itertools
import os

import numpy as np
import pandas as pd

from . import core
from . import utils


class Parallelizer(object):
    """Arguments:

    * ``target_dir``: path to output directory for parallel runs.
    * ``config_run``: path to YAML file with run settings. If not
      given, ``{{ module }}/config/run.yaml`` is used as the default.

    """
    def __init__(self, target_dir, config_run=None):
        super(Parallelizer, self).__init__()
        config_path = os.path.join(os.path.dirname(__file__), 'config')
        if not config_run:
            config_run = os.path.join(config_path, 'run.yaml')
        self.config_file = config_run
        self.config = utils.AttrDict.from_yaml(config_run)
        self.target_dir = target_dir
        self.f_submit = 'submit_{}.sh'
        self.f_run = 'run.sh'

    def generate_iterations(self):
        c = self.config.parallel.iterations
        if isinstance(c, list):
            df = pd.DataFrame(c)
        elif isinstance(c, dict):
            iter_keys = c.keys_nested()
            iter_values = [c.get_key(i) for i in iter_keys]
            df = pd.DataFrame(list(itertools.product(*iter_values)),
                              columns=iter_keys)
        df.index = range(1, len(df) + 1)  # 1 instead of 0-indexing
        return df

    def _write_modelcommands(self, f, settings):
        """
        Write model execution commands for the settings file ``settings``
        to file ``f``

        """
        pth = os.path.join('Runs', settings)
        f.write('python -c "import calliope\n')
        f.write('model = calliope.Model(config_run=\'{}\')\n'.format(pth))
        f.write('model.run()"\n')

    def _write_submit(self, f, n_iter, config=None):
        """
        Write submit script to file ``f``

        """
        if config is None:
            c = self.config
        else:
            c = config

        # Differentiate between singe and array styles
        if c.parallel.style == 'single':
            iter_string = '{}'.format(n_iter)
        elif c.parallel.style == 'array':
            iter_string = '1-{}'.format(n_iter)

        # Write the script
        f.write('#!/bin/sh\n')
        if c.parallel.environment == 'qsub':
            f.write('#$ -t {}\n'.format(iter_string))
            f.write('#$ -N {}\n'.format(c.parallel.name))
            f.write('#$ -j y -o Logs/run_$TASK_ID.log\n')
            self._write_resources(f, config)
            f.write('#$ -cwd\n')
            f.write('\n./{} '.format(self.f_run) + '${SGE_TASK_ID}\n')
        elif c.parallel.environment == 'bsub':
            f.write('#BSUB -J {}[{}]\n'.format(c.parallel.name, iter_string))
            self._write_resources(f, config)
            f.write('#BSUB -o Logs/run_%I.log\n')
            f.write('\n./{} '.format(self.f_run) + '${LSB_JOBINDEX}\n')

    def _write_resources(self, f, config=None):
        """
        Write resource requirements (memory, threads, wall time)
        to file ``f``

        """
        if config is None:
            c = self.config
        else:
            c = config

        if c.parallel.environment == 'qsub':
            conf = c.get_key('parallel.resources.memory', default=False)
            if conf:
                mem_gb = conf / 1000.0  # Convert MB in settings to GB
                f.write('#$ -l mem_total={:.1f}G\n'.format(mem_gb))

            conf = c.get_key('parallel.resources.threads', default=False)
            if conf:
                try:
                    penv = c.get_key('parallel.parallel_env')
                except:
                    raise KeyError('Must specify parallel_env for'
                                   'threads >1 and qsub.')
                f.write('#$ -pe {} {}\n'.format(penv, conf))
        elif c.parallel.environment == 'bsub':
            conf = c.get_key('parallel.resources.memory', default=False)
            if conf:
                f.write('#BSUB -R "rusage[mem={:.0f}]"\n'.format(conf))
            conf = c.get_key('parallel.resources.threads', default=False)
            if conf:
                f.write('#BSUB -n {:.0f}\n'.format(conf))
            conf = c.get_key('parallel.resources.wall_time', default=False)
            if conf:
                f.write('#BSUB -W {:.0f}\n'.format(conf))

    def _write_additional_lines(self, f):
        """Write additional lines (if configured) to file ``f``"""
        c = self.config
        lines = c.parallel.additional_lines
        if isinstance(lines, list):
            f.writelines([i + '\n' for i in lines])
        else:
            f.write(lines + '\n')

    def _get_iteration_config(self, config, index_str, iter_row):
        iter_c = copy.copy(config)  # iter_c is this iteration's config
        # `iteration_override` is a pandas series (dataframe row)
        for k, v in iter_row.to_dict().iteritems():
            # NaN values can show in this row if some but not all iterations
            # specify a value, so we simply skip them
            if pd.isnull(v):
                continue
            # Convert numpy dtypes to python ones, else YAML chokes
            if isinstance(v, np.generic):
                v = np.asscalar(v)
            iter_c.set_key(k, copy.copy(v))
        # Set output dir in configuration object, this is hardcoded
        iter_c.set_key('output.path', os.path.join('Output', index_str))
        return iter_c

    def generate_runs(self):
        c = self.config
        # Create output directory
        if self.target_dir:
            out_dir = os.path.join(self.target_dir, c.parallel.name)
        else:
            out_dir = c.parallel.name
        os.makedirs(out_dir)
        os.makedirs(os.path.join(out_dir, 'Runs'))
        os.makedirs(os.path.join(out_dir, 'Logs'))
        iterations = self.generate_iterations()
        # Save relevant settings in out_dir,
        # to figure out what all the numbers mean!
        os.makedirs(os.path.join(out_dir, 'Output'))
        iterations.to_csv(os.path.join(out_dir, 'Output', 'iterations.csv'))
        parallel_f = os.path.join(out_dir, 'Output', 'parallel_settings.yaml')
        c.parallel.to_yaml(parallel_f)

        # Decide whether to generate a single or multiple submission files
        # If different iterations ask for different resources, multiple
        # files are necessary
        if 'parallel' in c and 'resources' in c['parallel']:
            c.set_key('parallel.style', 'single')
        else:
            c.set_key('parallel.style', 'array')

        #
        # COMBINE ALL MODEL CONFIG INTO ONE FILE AND WRITE IT TO `out_dir`
        #
        o = core.get_model_config(c, self.config_file, adjust_data_path=False,
                                  insert_defaults=False)
        unified_config_file = os.path.join(out_dir, 'Runs', 'model.yaml')
        o.to_yaml(os.path.join(unified_config_file))
        c.input.model = 'model.yaml'
        # Always make sure we are saving outputs from iterations!
        c.set_key('output.save', True)

        #
        # SET UP SUBMISSION SCRIPT FOR ARRAY RUNS
        #
        if c.parallel.style == 'array':
            # Write array submission script
            submit_file = os.path.join(out_dir, self.f_submit.format('array'))
            with open(submit_file, 'w') as f:
                self._write_submit(f, n_iter=len(iterations))
            os.chmod(submit_file, 0755)

        #
        # SET UP RUN SCRIPT AND SUBMISSION SCRIPTS FOR SINGLE RUNS
        #
        run_file = os.path.join(out_dir, self.f_run)
        with open(run_file, 'w') as f:
            f.write('#!/bin/sh\n')
            f.write('case "$1" in\n\n')
        for iter_id, iter_row in iterations.iterrows():
            index_str = '{:0>4d}'.format(iter_id)
            iter_c = self._get_iteration_config(c, index_str, iter_row)
            settings_file = 'settings_{}.yaml'.format(index_str)

            # Write run script entry
            with open(os.path.join(out_dir, self.f_run), 'a') as f:
                f.write('{}) '.format(iter_id))
                if c.parallel.additional_lines:
                    self._write_additional_lines(f)
                self._write_modelcommands(f, settings_file)
                f.write(';;\n\n')

            # If style is single, also write a single submission script
            if c.parallel.style == 'single':
                # Write model run script
                submit_file = os.path.join(out_dir,
                                           self.f_submit.format(index_str))
                with open(submit_file, 'w') as f:
                    self._write_submit(f, n_iter=iter_id, config=iter_c)
                os.chmod(submit_file, 0755)

            # Write configuration object to YAML file
            del iter_c.parallel  # parallel settings not needed in each file
            iter_c.to_yaml(os.path.join(out_dir, 'Runs', settings_file))

        # Final tasks after going through all iterations
        with open(run_file, 'a') as f:
            f.write('esac\n')
        os.chmod(run_file, 0755)
