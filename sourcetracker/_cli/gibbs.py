#!/usr/bin/env python
# ----------------------------------------------------------------------------
# Copyright (c) 2016--, Biota Technology.
# www.biota.com
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from __future__ import division

import os
import click
import subprocess
import time
import pandas as pd
from biom import load_table
from sourcetracker._cli import cli
from sourcetracker._sourcetracker import (biom_to_df, _gibbs, _gibbs_loo,
                                          intersect_and_sort_samples,
                                          get_samples, collapse_source_data,
                                          subsample_dataframe)
from ipyparallel import Client


@cli.command(name='gibbs')
@click.option('-i', '--table_fp', required=True,
              type=click.Path(exists=True, dir_okay=False),
              help='Path to input BIOM table.')
@click.option('-m', '--mapping_fp', required=True,
              type=click.Path(exists=True, dir_okay=False),
              help='Path to sample metadata mapping file.')
@click.option('-o', '--output_dir', required=True,
              type=click.Path(exists=False, dir_okay=False),
              help='Path to the output directory to be created.')
@click.option('--loo', required=False, default=False, is_flag=True,
              show_default=True,
              help=('Classify each sample in `sources` using a leave-one-out '
                    'strategy. Replicates -s option in Knights et al. '
                    'sourcetracker.'))
@click.option('--jobs', required=False, default=1,
              type=click.INT, show_default=True,
              help='Number of processes to launch.')
@click.option('--alpha1', required=False, default=.001,
              type=click.FLOAT, show_default=True,
              help=('Prior counts of each species in the training '
                    'environments. Higher values decrease the trust in the '
                    'training environments, and make the source environment '
                    'distrubitons over taxa smoother. By default, this is '
                    'set to 0.001, which indicates reasonably high trust in '
                    'all source environments, even those with few training '
                    'sequences. This is useful when only a small number of '
                    'biological samples are available from a source '
                    'environment. A more conservative value would be 0.01.'))
@click.option('--alpha2', required=False, default=.001,
              type=click.FLOAT, show_default=True,
              help=('Prior counts of each species in Unknown environment. '
                    'Higher values make the Unknown environment smoother and '
                    'less prone to overfitting given a training sample.'))
@click.option('--beta', required=False, default=10,
              type=click.INT, show_default=True,
              help=('Count to be added to each species in each environment, '
                    'including `unknown`.'))
@click.option('--source_rarefaction_depth', required=False, default=1000,
              type=click.IntRange(min=0, max=None), show_default=True,
              help=('Depth at which to rarify sources. If 0, no '
                    'rarefaction performed.'))
@click.option('--sink_rarefaction_depth', required=False, default=1000,
              type=click.IntRange(min=0, max=None), show_default=True,
              help=('Depth at which to rarify sinks. If 0, no '
                    'rarefaction performed.'))
@click.option('--restarts', required=False, default=10,
              type=click.INT, show_default=True,
              help=('Number of independent Markov chains to grow. '
                    '`draws_per_restart` * `restarts` gives the number of '
                    'samplings of the mixing proportions that will be '
                    'generated.'))
@click.option('--draws_per_restart', required=False, default=1,
              type=click.INT, show_default=True,
              help=('Number of times to sample the state of the Markov chain '
                    'for each independent chain grown.'))
@click.option('--burnin', required=False, default=100,
              type=click.INT, show_default=True,
              help=('Number of passes (withdarawal and reassignment of every '
                    'sequence in the sink) that will be made before a sample '
                    '(draw) will be taken. Higher values allow more '
                    'convergence towards the true distribtion before draws '
                    'are taken.'))
@click.option('--delay', required=False, default=10,
              type=click.INT, show_default=True,
              help=('Number passes between each sampling (draw) of the '
                    'Markov chain. Once the burnin passes have been made, a '
                    'sample will be taken every `delay` number of passes. '
                    'This is also known as `thinning`. Thinning helps reduce '
                    'the impact of correlation between adjacent states of the '
                    'Markov chain.'))
@click.option('--cluster_start_delay', required=False, default=25,
              type=click.INT, show_default=True,
              help=('When using multiple jobs, the script has to start an '
                    '`ipcluster`. If ipcluster does not recognize that it '
                    'has been successfully started, the jobs will not be '
                    'successfully launched. If this is happening, increase '
                    'this parameter.'))
@click.option('--per_sink_feature_assignments', required=False, default=False,
              is_flag=True, show_default=True,
              help=('If True, this option will cause SourceTracker2 to write '
                    'out a feature table for each sink (or source if `--loo` '
                    'is passed). These feature tables contain the specific '
                    'sequences that contributed to a sink from a given '
                    'source. This option can be memory intensive if there are '
                    'a large number of features.'))
@click.option('--source_sink_column', required=False, default='SourceSink',
              type=click.STRING, show_default=True,
              help=('Sample metadata column indicating which samples should be'
                    ' treated as sources and which as sinks.'))
@click.option('--source_column_value', required=False, default='source',
              type=click.STRING, show_default=True,
              help=('Value in source_sink_column indicating which samples '
                    'should be treated as sources.'))
@click.option('--sink_column_value', required=False, default='sink',
              type=click.STRING, show_default=True,
              help=('Value in source_sink_column indicating which samples '
                    'should be treated as sinks.'))
@click.option('--source_category_column', required=False, default='Env',
              type=click.STRING, show_default=True,
              help=('Sample metadata column indicating the type of each '
                    'source sample.'))
def gibbs(table_fp, mapping_fp, output_dir, loo, jobs, alpha1, alpha2, beta,
          source_rarefaction_depth, sink_rarefaction_depth, restarts,
          draws_per_restart, burnin, delay, cluster_start_delay,
          per_sink_feature_assignments, source_sink_column,
          source_column_value, sink_column_value, source_category_column):
    '''Gibb's sampler for Bayesian estimation of microbial sample sources.

    For details, see the project README file.
    '''
    # Create results directory. Click has already checked if it exists, and
    # failed if so.
    os.mkdir(output_dir)

    # Load the metadata file and feature table. Do a data check on the
    # feature_table.
    sample_metadata = pd.read_table(mapping_fp, sep='\t', dtype=object)
    sample_metadata.set_index(sample_metadata.columns[0], drop=True,
                              append=False, inplace=True)
    _ft = load_table(table_fp)
    feature_table = biom_to_df(_ft, apply_fractional_value_correction=True)

    # Remove samples not shared by both feature and metadata tables and order
    # rows equivalently.
    sample_metadata, feature_table = \
        intersect_and_sort_samples(sample_metadata, feature_table)

    # Identify source and sink samples.
    source_samples = get_samples(sample_metadata, source_sink_column,
                                 source_column_value)
    sink_samples = get_samples(sample_metadata, source_sink_column,
                               sink_column_value)

    # If we have no source samples neither normal operation or loo will work.
    # Will also likely get strange errors.
    if len(source_samples) == 0:
        raise ValueError(('You passed %s as the `source_sink_column` and %s '
                          'as the `source_column_value`. There are no samples '
                          'which are sources under these values. Please see '
                          'the help documentation and check your mapping '
                          'file.') % (source_sink_column, source_column_value))

    # Prepare the 'sources' matrix by collapsing the `source_samples` by their
    # metadata values.
    csources = collapse_source_data(sample_metadata, feature_table,
                                    source_samples, source_category_column,
                                    'mean')

    # Rarify collapsed source data if requested.
    if source_rarefaction_depth > 0:
        d = (csources.sum(1) >= source_rarefaction_depth)
        if not d.all():
            print(csources.sum(1))
            too_shallow = (~d).sum()
            shallowest = csources.sum(1).min()
            raise ValueError(('You requested rarefaction of source samples at '
                              '%s, but there are %s collapsed source samples '
                              'that have less sequences than that. The '
                              'shallowest of these is %s sequences.') %
                             (source_rarefaction_depth, too_shallow,
                              shallowest))
        else:
            csources = subsample_dataframe(csources, source_rarefaction_depth)

    # Prepare sink data and rarify if requested.
    sinks = feature_table.loc[sink_samples, :]
    if sink_rarefaction_depth > 0:
        d = (sinks.sum(1) >= sink_rarefaction_depth)
        if not d.all():
            too_shallow = (~d).sum()
            shallowest = sinks.sum(1).min()
            raise ValueError(('You requested rarefaction of sink samples at '
                              '%s, but there are %s sink samples that have '
                              'less sequences than that. The shallowest of '
                              'these is %s sequences.') %
                             (sink_rarefaction_depth, too_shallow,
                              shallowest))
        else:
            sinks = subsample_dataframe(sinks, sink_rarefaction_depth)

    # If we've been asked to do multiple jobs, we need to spin up a cluster.
    if jobs > 1:
        # Launch the ipcluster and wait for it to come up.
        subprocess.Popen('ipcluster start -n %s --quiet' % jobs, shell=True)
        time.sleep(cluster_start_delay)
        cluster = Client()
    else:
        cluster = None

    # Run the computations.
    if not loo:
        mpm, mps, fas = \
            _gibbs(csources, sinks, alpha1, alpha2, beta, restarts,
                   draws_per_restart, burnin, delay, cluster=cluster,
                   create_feature_tables=per_sink_feature_assignments)
    else:
        mpm, mps, fas = \
            _gibbs_loo(csources, alpha1, alpha2, beta, restarts,
                       draws_per_restart, burnin, delay, cluster=cluster,
                       create_feature_tables=per_sink_feature_assignments)
    # If we started a cluster, shut it down.
    if jobs > 1:
        cluster.shutdown(hub=True)

    # Write results.
    mpm.to_csv(os.path.join(output_dir, 'mixing_proportions.txt'), sep='\t')
    mps.to_csv(os.path.join(output_dir, 'mixing_proportions_stds.txt'),
               sep='\t')
    if per_sink_feature_assignments:
        for sink, fa in zip(mpm.index, fas):
            fa.to_csv(os.path.join(output_dir, sink + '.feature_table.txt'),
                      sep='\t')
