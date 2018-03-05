import os.path
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + '../../')

import numpy as np
import math

import torch
from torch.autograd import Variable

from pydeformetrica.src.in_out.array_readers_and_writers import *
from pydeformetrica.src.core.models.abstract_statistical_model import AbstractStatisticalModel
from pydeformetrica.src.support.utilities.general_settings import Settings
from pydeformetrica.src.support.probability_distributions.inverse_wishart_distribution import InverseWishartDistribution
from pydeformetrica.src.support.probability_distributions.multi_scalar_inverse_wishart_distribution import \
    MultiScalarInverseWishartDistribution
from pydeformetrica.src.support.probability_distributions.multi_scalar_normal_distribution import \
    MultiScalarNormalDistribution
import matplotlib.pyplot as plt
from matplotlib.colors import cnames

class LongitudinalMetricLearning(AbstractStatisticalModel):
    """
    Deterministic atlas object class.

    """

    ####################################################################################################################
    ### Constructor:
    ####################################################################################################################

    def __init__(self):
        AbstractStatisticalModel.__init__(self)

        self.number_of_subjects = None
        self.number_of_objects = None

        self.geodesic = None

        # Dictionary of numpy arrays.
        self.fixed_effects['p0'] = None
        self.fixed_effects['reference_time'] = None
        self.fixed_effects['v0'] = None
        self.fixed_effects['onset_age_variance'] = None
        self.fixed_effects['log_acceleration_variance'] = None
        self.fixed_effects['noise_variance'] = None

        # Dictionary of prior distributions
        self.priors['onset_age_variance'] = MultiScalarInverseWishartDistribution()
        self.priors['log_acceleration_variance'] = MultiScalarInverseWishartDistribution()
        self.priors['noise_variance'] = MultiScalarInverseWishartDistribution()

        # Dictionary of probability distributions.
        self.individual_random_effects['sources'] = MultiScalarNormalDistribution()
        self.individual_random_effects['onset_age'] = MultiScalarNormalDistribution()
        self.individual_random_effects['log_acceleration'] = MultiScalarNormalDistribution()

        # Dictionary of booleans
        self.is_frozen = {}
        self.is_frozen['v0'] = False
        self.is_frozen['p0'] = False
        self.is_frozen['reference_time'] = False
        self.is_frozen['onset_age_variance'] = False
        self.is_frozen['log_acceleration_variance'] = False
        self.is_frozen['noise_variance'] = False
        self.is_frozen['metric_parameters'] = False
        self.is_frozen['modulation_matrix'] = False

    ####################################################################################################################
    ### Encapsulation methods:
    ####################################################################################################################

    def set_v0(self, v0):
        self.fixed_effects['v0'] = np.array([v0]).flatten()

    def set_p0(self, p0):
        self.fixed_effects['p0'] = np.array([p0]).flatten()

    # Reference time ---------------------------------------------------------------------------------------------------
    def get_reference_time(self):
        return self.fixed_effects['reference_time']

    def set_reference_time(self, rt):
        self.fixed_effects['reference_time'] = np.float64(rt)
        self.individual_random_effects['onset_age'].mean = np.array([rt])

    def get_modulation_matri(self):
        return self.fixed_effects['modulation_matrix']

    def set_modulation_matrix(self, mm):
        self.fixed_effects['modulation_matrix'] = mm

    # Log-acceleration variance ----------------------------------------------------------------------------------------
    def get_log_acceleration_variance(self):
        return self.fixed_effects['log_acceleration_variance']

    def set_log_acceleration_variance(self, lav):
        self.fixed_effects['log_acceleration_variance'] = np.float64(lav)
        self.individual_random_effects['log_acceleration'].set_variance(lav)

    # Time-shift variance ----------------------------------------------------------------------------------------------
    def get_onset_age_variance(self):
        return self.fixed_effects['onset_age_variance']

    def set_onset_age_variance(self, tsv):
        self.fixed_effects['onset_age_variance'] = np.float64(tsv)
        self.individual_random_effects['onset_age'].set_variance(tsv)

    # Noise variance ---------------------------------------------------------------------------------------------------
    def get_noise_variance(self):
        return self.fixed_effects['noise_variance']

    def set_noise_variance(self, nv):
        self.fixed_effects['noise_variance'] = np.float64(nv)

    def get_metric_parameters(self):
        return self.fixed_effects['metric_parameters']

    def set_metric_parameters(self, metric_parameters):
        """
        Reproject the metric parameters to guarantee the identifiability
        It also creates the 'metric_parameters' key in the fixed effects, in case the metric does not have parameters.
        """
        for i in range(len(metric_parameters)):
            if metric_parameters[i] < 0:
                metric_parameters[i] = 0

        metric_parameters /= np.sum(metric_parameters)

        self.fixed_effects['metric_parameters'] = metric_parameters

    # Full fixed effects -----------------------------------------------------------------------------------------------
    def get_fixed_effects(self):
        out = {}
        if not self.is_frozen['p0']:
            out['p0'] = np.array([self.fixed_effects['p0']])
        if not self.is_frozen['v0']:
            out['v0'] = np.array([self.fixed_effects['v0']])
        if not self.is_frozen['metric_parameters']:
            out['metric_parameters'] = self.fixed_effects['metric_parameters']
        # if not self.
        return out

    def set_fixed_effects(self, fixed_effects):
        if not self.is_frozen['p0']: self.set_p0(fixed_effects['p0'])
        if not self.is_frozen['v0']: self.set_v0(fixed_effects['v0'])
        if not self.is_frozen['metric_parameters']: self.set_metric_parameters(fixed_effects['metric_parameters'])

    ####################################################################################################################
    ### Public methods:
    ####################################################################################################################

    def update(self):
        """
        Initializations of prior parameters
        """
        self.initialize_noise_variables()
        self.initialize_onset_age_variables()
        self.initialize_log_acceleration_variables()

    # Compute the functional. Numpy input/outputs.
    def compute_log_likelihood(self, dataset, population_RER, individual_RER, mode='complete', with_grad=False):
        """
        Compute the log-likelihood of the dataset, given parameters fixed_effects and random effects realizations
        population_RER and indRER.

        :param dataset: LongitudinalDataset instance
        :param fixed_effects: Dictionary of fixed effects.
        :param population_RER: Dictionary of population random effects realizations.
        :param indRER: Dictionary of individual random effects realizations.
        :param mode: Indicates which log_likelihood should be computed, between 'complete', 'model', and 'class2'.
        :param with_grad: Flag that indicates wether the gradient should be returned as well.
        :return:
        """
        v0, p0, metric_parameters = self._fixed_effects_to_torch_tensors(with_grad)
        onset_ages, log_accelerations = self._individual_RER_to_torch_tensors(individual_RER, with_grad)

        # Sanity check (happens with extreme line searches)
        if p0.data.numpy()[0] > 1. or p0.data.numpy()[0] < 0.:
            raise ValueError("Absurd p0 value in compute_log_likelihood. Exception raised.")

        residuals = self._compute_residuals(dataset, v0, p0, log_accelerations, onset_ages, metric_parameters)

        #Achtung update of the metric parameters
        if mode == 'complete':
            sufficient_statistics = self.compute_sufficient_statistics(dataset, population_RER, individual_RER, residuals)
            self.update_fixed_effects(dataset, sufficient_statistics)

        attachments = self._compute_individual_attachments(residuals)
        attachment = torch.sum(attachments)

        regularity = self._compute_random_effects_regularity(log_accelerations, onset_ages)
        if mode == 'complete':
            regularity += self._compute_class1_priors_regularity()
            regularity += self._compute_class2_priors_regularity()
        if mode in ['complete', 'class2']:
            regularity += self._compute_class2_priors_regularity()

        if with_grad:
            total = attachment + regularity
            total.backward(retain_graph=True)

            # Gradients of the effects with no closed form update.
            gradient = {}
            if not self.is_frozen['v0']: gradient['v0'] = v0.grad.data.cpu().numpy()
            if not self.is_frozen['p0']: gradient['p0'] = p0.grad.data.cpu().numpy()
            if not self.is_frozen['metric_parameters']:
                gradient['metric_parameters'] = metric_parameters.grad.data.cpu().numpy()
                # #We project the gradient of the metric parameters onto the orthogonal of the constraint.
                orthogonal_gradient = np.ones(len(gradient['metric_parameters']))
                orthogonal_gradient /= np.linalg.norm(orthogonal_gradient)
                gradient['metric_parameters'] -= np.dot(gradient['metric_parameters'], orthogonal_gradient) * orthogonal_gradient
                sp = abs(np.dot(gradient['metric_parameters'], orthogonal_gradient))
                assert sp < 1e-6, "Gradient incorrectly projected %f" %sp

            if mode == 'complete':
                gradient['onset_age'] = onset_ages.grad.data.cpu().numpy()
                gradient['log_acceleration'] = log_accelerations.grad.data.cpu().numpy()

            if mode in ['complete', 'class2']:
                return attachment.data.cpu().numpy()[0], regularity.data.cpu().numpy()[0], gradient
            elif mode == 'model':
                return attachments.data.cpu().numpy(), gradient

        else:
            if mode in ['complete', 'class2']:
                return attachment.data.cpu().numpy()[0], regularity.data.cpu().numpy()[0]
            elif mode == 'model':
                return attachments.data.cpu().numpy()

    def _fixed_effects_to_torch_tensors(self, with_grad):
        v0_torch = Variable(torch.from_numpy(self.fixed_effects['v0']),
                            requires_grad=((not self.is_frozen['v0']) and with_grad))\
            .type(Settings().tensor_scalar_type)

        p0_torch = Variable(torch.from_numpy(self.fixed_effects['p0']),
                            requires_grad=((not self.is_frozen['p0']) and with_grad))\
            .type(Settings().tensor_scalar_type)

        if self.geodesic.manifold_type in ['parametric']:
            metric_parameters = Variable(torch.from_numpy(
                self.fixed_effects['metric_parameters']),
                requires_grad=((not self.is_frozen['metric_parameters']) and with_grad))\
                .type(Settings().tensor_scalar_type)

            return v0_torch, p0_torch, metric_parameters

        return v0_torch, p0_torch, None

    def _individual_RER_to_torch_tensors(self, individual_RER, with_grad):
        onset_ages = Variable(torch.from_numpy(individual_RER['onset_age']).type(Settings().tensor_scalar_type),
                              requires_grad=with_grad)
        log_accelerations = Variable(torch.from_numpy(individual_RER['log_acceleration']).type(Settings().tensor_scalar_type),
                                     requires_grad=with_grad)

        return onset_ages, log_accelerations

    def _compute_residuals(self, dataset, v0, p0, log_accelerations, onset_ages, metric_parameters=None):
        targets = dataset.deformable_objects # A list of list
        absolute_times = self._compute_absolute_times(dataset.times, log_accelerations, onset_ages)

        residuals = []

        t0 = self.get_reference_time()

        self.geodesic.set_t0(t0)
        self.geodesic.set_position_t0(p0)
        self.geodesic.set_tmin(min([subject_times[0].data.numpy()[0] for subject_times in absolute_times] + [t0]))
        self.geodesic.set_tmax(max([subject_times[-1].data.numpy()[0] for subject_times in absolute_times] + [t0]))

        if metric_parameters is not None:
            for val in metric_parameters.data.numpy():
                if val < 0:
                    raise ValueError('Absurd metric parameter value in compute residuals. Exception raised.')
            self.geodesic.set_parameters(metric_parameters)

        self.geodesic.set_velocity_t0(v0)

        self.geodesic.update()

        number_of_subjects = dataset.number_of_subjects
        for i in range(number_of_subjects):
            residuals_i = torch.zeros_like(absolute_times[i])
            for j, (time, target) in enumerate(zip(absolute_times[i], targets[i])):
                predicted_value = self.geodesic.get_geodesic_point(absolute_times[i][j])
                residuals_i[j] = (target - predicted_value)**2
            residuals.append(residuals_i)
        return residuals

    def _compute_individual_attachments(self, residuals):

        total_residual = 0

        number_of_subjects = len(residuals)
        attachments = Variable(torch.zeros((number_of_subjects,)).type(Settings().tensor_scalar_type),
                               requires_grad=False)
        noise_variance_torch = Variable(torch.from_numpy(np.array([self.fixed_effects['noise_variance']]))
                                        .type(Settings().tensor_scalar_type),
                                        requires_grad=False)

        for i in range(number_of_subjects):
            attachments[i] = -0.5 * torch.sum(residuals[i]) / noise_variance_torch

        return attachments

    def _compute_absolute_times(self, times, log_accelerations, onset_ages):
        """
        Fully torch.
        """
        reference_time = self.get_reference_time()
        accelerations = torch.exp(log_accelerations)

        upper_threshold = 500.
        lower_threshold = 1e-5
        print("Acceleration factor max:", np.max(accelerations.data.numpy()), np.argmax(accelerations.data.numpy()))
        # print("Acceleration factor min:", np.min(accelerations.data.numpy()), np.argmin(accelerations.data.numpy()))
        if np.max(accelerations.data.numpy()) > upper_threshold or np.min(accelerations.data.numpy()) < lower_threshold:
            raise ValueError('Absurd numerical value for the acceleration factor. Exception raised.')

        absolute_times = []
        for i in range(len(times)):
            absolute_times_i = (Variable(torch.from_numpy(times[i]).type(Settings().tensor_scalar_type)) - onset_ages[i]) * accelerations[i] + reference_time
            absolute_times.append(absolute_times_i)
        return absolute_times

    def _compute_absolute_time(self, time, acceleration, onset_age, reference_time):
        return (Variable(torch.from_numpy(np.array([time])).type(Settings().tensor_scalar_type)) - onset_age) * acceleration + reference_time

    ####################################################################################################################
    ### Private methods:
    ####################################################################################################################

    def _compute_random_effects_regularity(self, log_accelerations, onset_ages):
        """
        Fully torch.
        """
        number_of_subjects = onset_ages.shape[0]
        regularity = 0.0

        # Onset age random effect.
        for i in range(number_of_subjects):
            regularity += self.individual_random_effects['onset_age'].compute_log_likelihood_torch(onset_ages[i])

        # Log-acceleration random effect.
        for i in range(number_of_subjects):
            regularity += \
                self.individual_random_effects['log_acceleration'].compute_log_likelihood_torch(log_accelerations[i])

        # Noise random effect.
        regularity -= 0.5 * number_of_subjects \
                      * math.log(self.fixed_effects['noise_variance'])

        return regularity

    def compute_sufficient_statistics(self, dataset, population_RER, individual_RER, residuals=None):
        sufficient_statistics = {}

        if residuals is None:
            v0, p0, metric_parameters = self._fixed_effects_to_torch_tensors(False)

            onset_ages, log_accelerations = self._individual_RER_to_torch_tensors(individual_RER, False)

            residuals = self._compute_residuals(dataset, v0, p0, log_accelerations, onset_ages, metric_parameters)

        if not self.is_frozen['noise_variance']:
            sufficient_statistics['S1'] = 0.
            for i in range(len(residuals)):
                sufficient_statistics['S1'] += torch.sum(residuals[i]).data.numpy()[0]

        if not self.is_frozen['log_acceleration_variance']:
            log_accelerations = individual_RER['log_acceleration']
            sufficient_statistics['S2'] = np.sum(log_accelerations**2)

        if not self.is_frozen['reference_time'] or not self.is_frozen['onset_age_variance']:
            onset_ages = individual_RER['onset_age']
            sufficient_statistics['S3'] = np.sum(onset_ages)

        if not self.is_frozen['onset_age_variance']:
            log_accelerations = individual_RER['log_acceleration']
            ref_time = sufficient_statistics['S3']/dataset.number_of_subjects
            sufficient_statistics['S4'] = np.sum((onset_ages - ref_time)**2)

        return sufficient_statistics

    def update_fixed_effects(self, dataset, sufficient_statistics):
        """
        Updates the fixed effects based on the sufficient statistics, maximizing the likelihood.
        """
        number_of_subjects = dataset.number_of_subjects
        total_number_of_observations = dataset.total_number_of_observations

        # Updating the noise variance
        if not self.is_frozen['noise_variance']:
            prior_scale = self.priors['noise_variance'].scale_scalars[0]
            prior_dof = self.priors['noise_variance'].degrees_of_freedom[0]
            noise_variance = (sufficient_statistics['S1'] + prior_dof * prior_scale) \
                                        / (total_number_of_observations + prior_dof) # Dimension of objects is 1
            self.set_noise_variance(noise_variance)

        # Updating the log acceleration variance
        if not self.is_frozen['log_acceleration_variance']:
            prior_scale = self.priors['log_acceleration_variance'].scale_scalars[0]
            prior_dof = self.priors['log_acceleration_variance'].degrees_of_freedom[0]
            log_acceleration_variance = (sufficient_statistics["S2"] + prior_dof * prior_scale) \
                                        / (number_of_subjects + prior_dof)
            self.set_log_acceleration_variance(log_acceleration_variance)
            print("log acceleration variance", log_acceleration_variance)
            print("Un-regularized log acceleration variance : ", sufficient_statistics['S2']/number_of_subjects)

        # Updating the reference time
        if not self.is_frozen['reference_time']:
            reftime = sufficient_statistics['S3']/number_of_subjects
            self.set_reference_time(reftime)

        # Updating the onset ages variance
        if not self.is_frozen['onset_age_variance']:
            reftime = self.get_reference_time()
            onset_age_prior_scale = self.priors['onset_age_variance'].scale_scalars[0]
            onset_age_prior_dof = self.priors['onset_age_variance'].degrees_of_freedom[0]
            onset_age_variance = (sufficient_statistics['S4'] + onset_age_prior_dof * onset_age_prior_scale) \
                                  / (number_of_subjects + onset_age_prior_scale)
            self.set_onset_age_variance(onset_age_variance)

    def _compute_class1_priors_regularity(self):
        """
        Fully torch.
        Prior terms of the class 1 fixed effects, i.e. those for which we know a close-form update. No derivative
        wrt those fixed effects will therefore be necessary.
        """
        regularity = 0.0

        # Time-shift variance prior
        if not self.is_frozen['onset_age_variance']:
            regularity += \
                self.priors['onset_age_variance'].compute_log_likelihood(self.fixed_effects['onset_age_variance'])

        # Log-acceleration variance prior
        if not self.is_frozen['log_acceleration_variance']:
            regularity += self.priors['log_acceleration_variance'].compute_log_likelihood(
                self.fixed_effects['log_acceleration_variance'])

        # Noise variance prior
        if not self.is_frozen['noise_variance']:
            regularity += self.priors['noise_variance'].compute_log_likelihood(self.fixed_effects['noise_variance'])

        return regularity

    def _compute_class2_priors_regularity(self):
        """
        Fully torch.
        Prior terms of the class 2 fixed effects, i.e. those for which we do not know a close-form update. Derivative
        wrt those fixed effects will therefore be necessary.
        """
        regularity = 0.0
        # We don't have regularity terms on \theta_g, A, v0 or p0.

        return Variable(torch.Tensor([regularity]).type(Settings().tensor_scalar_type))

    def initialize_noise_variables(self):
        initial_noise_variance = self.get_noise_variance()
        assert initial_noise_variance > 0
        if len(self.priors['noise_variance'].scale_scalars) == 0:
                self.priors['noise_variance'].scale_scalars.append(initial_noise_variance)

    def initialize_onset_age_variables(self):
        # Check that the onset age random variable mean has been set.
        if self.individual_random_effects['onset_age'].mean is None:
            raise RuntimeError('The set_reference_time method of a LongitudinalAtlas model should be called before '
                               'the update one.')
        # Check that the onset age random variable variance has been set.
        if self.individual_random_effects['onset_age'].variance_sqrt is None:
            raise RuntimeError('The set_time_shift_variance method of a LongitudinalAtlas model should be called '
                               'before the update one.')

        if not self.is_frozen['onset_age_variance']:
            # Set the time_shift_variance prior scale to the initial time_shift_variance fixed effect.
            self.priors['onset_age_variance'].scale_scalars.append(self.get_onset_age_variance())
            print('>> The time shift variance prior degrees of freedom parameter is ARBITRARILY set to 0.0001')
            self.priors['onset_age_variance'].degrees_of_freedom.append(1.)

    def initialize_log_acceleration_variables(self):
        # Set the log_acceleration random variable mean.
        self.individual_random_effects['log_acceleration'].mean = np.zeros((1,))
        # Set the log_acceleration_variance fixed effect.
        if self.get_log_acceleration_variance() is None:
            print('>> The initial log-acceleration std fixed effect is ARBITRARILY set to 0.5')
            log_acceleration_std = 0.5
            self.set_log_acceleration_variance(log_acceleration_std ** 2)

        if not self.is_frozen["log_acceleration_variance"]:
            # Set the log_acceleration_variance prior scale to the initial log_acceleration_variance fixed effect.
            self.priors['log_acceleration_variance'].scale_scalars.append(self.get_log_acceleration_variance()*0.01)
            # Arbitrarily set the log_acceleration_variance prior dof to 1.
            print('>> The log-acceleration variance prior degrees of '
                  'freedom parameter is ARBITRARILY set to the number of subjects:', self.number_of_subjects)
            self.priors['log_acceleration_variance'].degrees_of_freedom.append(self.number_of_subjects)

    ####################################################################################################################
    ### Writing methods:
    ####################################################################################################################

    def write(self, dataset, population_RER, individual_RER, sample=False, update_fixed_effects=False):
        self._write_model_predictions(dataset, individual_RER, sample=sample)
        self._write_model_parameters(individual_RER)
        self.geodesic.save_metric_plot()
        self.geodesic.save_geodesic_plot(name=self.name)
        self._write_individual_RER(dataset, individual_RER)

    def _write_model_parameters(self, individual_RER):
        # Metric parameters
        if 'metric_parameters' in self.fixed_effects.keys():
            metric_parameters = self.fixed_effects['metric_parameters']
            write_2D_array(metric_parameters, self.name + "_metric_parameters.txt")

        # Individual_RER
        onset_ages = individual_RER['onset_age']
        write_2D_array(onset_ages, self.name + "_onset_ages.txt")
        alphas = np.exp(individual_RER['log_acceleration'])
        write_2D_array(alphas, self.name + "_alphas.txt")
        write_2D_array(individual_RER['log_acceleration'], self.name + "_log_accelerations.txt")

        all_fixed_effects  = self.fixed_effects

        np.save(os.path.join(Settings().output_dir, self.name + "_all_fixed_effects.npy"), all_fixed_effects)

    def _write_individual_RER(self, dataset, individual_RER):
        onset_ages = individual_RER['onset_age']
        write_2D_array(onset_ages, self.name + "_onset_ages.txt")
        alphas = np.exp(individual_RER['log_acceleration'])
        write_2D_array(alphas, self.name + "_alphas.txt")
        write_2D_array(np.array(dataset.subject_ids), self.name + "_subject_ids_unique.txt", fmt='%s')

    def _write_model_predictions(self, dataset, individual_RER, sample=False):
        """
        Compute the model predictions
        if sample is On, it will compute predictions, noise them and save them
        else it will save the predictions.
        """

        v0, p0, metric_parameters = self._fixed_effects_to_torch_tensors(False)
        onset_ages, log_accelerations = self._individual_RER_to_torch_tensors(individual_RER, False)

        accelerations = torch.exp(log_accelerations)
        absolute_times = self._compute_absolute_times(dataset.times, log_accelerations, onset_ages)

        t0 = self.get_reference_time()

        self.geodesic.set_t0(t0)
        self.geodesic.set_position_t0(p0)
        self.geodesic.set_tmin(min([subject_times[0].data.numpy()[0]
                                    for subject_times in absolute_times] + [t0]))
        self.geodesic.set_tmax(max([subject_times[-1].data.numpy()[0]
                                    for subject_times in absolute_times] + [t0]))

        if metric_parameters is not None:
            for val in metric_parameters.data.numpy():
                if val < 0:
                    raise ValueError('Absurd metric parameter value in compute residuals. Exception raised.')

            self.geodesic.set_parameters(metric_parameters)

        self.geodesic.set_velocity_t0(v0)

        self.geodesic.update()

        colors = ['navy', 'orchid', 'tomato', 'grey', 'blue', 'green', 'red', 'cyan', 'magenta', 'yellow', 'black', 'maroon']

        # colors = []
        # for name in cnames.keys():
        #     if len(colors) < 12: colors.append(name)
        #     else: break

        pos = 0
        nb_plot_to_make = 3

        predictions = []
        subject_ids = []
        times = []

        if sample:
            targets = []
        else:
            targets = dataset.deformable_objects

        number_of_subjects = dataset.number_of_subjects
        for i in range(number_of_subjects):
            predictions_i = []
            for j, time in enumerate(absolute_times[i]):
                predicted_value = self.geodesic.get_geodesic_point(time)
                predictions_i.append(predicted_value.data.numpy()[0])
                predictions.append(predicted_value.data.numpy()[0])
                subject_ids.append(dataset.subject_ids[i])
                times.append(dataset.times[i][j])

            # Now plotting the real data.
            if sample:
                targets_i = np.array(predictions_i) + np.random.normal(0., np.sqrt(self.get_noise_variance()),
                                                                       size=len(predictions_i))
                for elt in targets_i:
                    targets.append(elt)
            else:
                targets_i = targets[i].data.numpy()

            if nb_plot_to_make > 0:
                # We also make a plot of the trajectory and save it.
                times_subject = np.linspace(dataset.times[i][0], dataset.times[i][-1], 100)
                absolute_times_subject = [self._compute_absolute_time(t, accelerations[i], onset_ages[i], t0) for t in times_subject]
                trajectory = [self.geodesic.get_geodesic_point(t).data.numpy()[0] for t in absolute_times_subject]
                plt.plot(times_subject, trajectory, c=colors[pos], label='subject ' + str(dataset.subject_ids[i]))

                plt.scatter([t for t in dataset.times[i]], [t for t in targets_i], color=colors[pos])
                pos += 1
                if pos >= len(colors) or i == number_of_subjects - 1:
                    plt.legend()
                    plt.savefig(os.path.join(Settings().output_dir, "plot_subject_"+str(i-pos+1)+'_to_'+str(i)+'.pdf'))
                    plt.clf()
                    pos = 0
                    nb_plot_to_make -= 1

        if sample:
            # Saving the generated value, noised
            write_2D_array(np.array(targets), self.name + "_generated_values.txt")
        else:
            # Saving the predictions, un-noised
            write_2D_array(np.array(predictions), self.name + "_reconstructed_values.txt")

        write_2D_array(np.array(subject_ids), self.name + "_subject_ids.txt", fmt='%s')
        write_2D_array(np.array(times), self.name + "_times.txt")

    def print(self, individual_RER):
        print('>> Model parameters:')

        # Noise variance.
        msg = '\t\t noise_variance    ='
        noise_variance = self.get_noise_variance()
        msg += '\t%.4f\t ; ' % (math.sqrt(noise_variance))
        print(msg[:-4])

        # Empirical distributions of the individual parameters.
        print('\t\t onset_ages        =\t%.3f\t[ mean ]\t+/-\t%.4f\t[std]' %
              (np.mean(individual_RER['onset_age']), np.std(individual_RER['onset_age'])))
        print('\t\t log_accelerations =\t%.4f\t[ mean ]\t+/-\t%.4f\t[std]' %
              (np.mean(individual_RER['log_acceleration']), np.std(individual_RER['log_acceleration'])))