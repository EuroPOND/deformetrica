import os.path
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + '../../../')

import numpy as np
from scipy.optimize import minimize
import _pickle as pickle
from pydeformetrica.src.core.estimators.abstract_estimator import AbstractEstimator
from pydeformetrica.src.support.utilities.general_settings import Settings


class ScipyOptimize(AbstractEstimator):
    """
    ScipyOptimize object class.
    An estimator is an algorithm which updates the fixed effects of a statistical model.

    """

    ####################################################################################################################
    ### Constructor:
    ####################################################################################################################

    def __init__(self):
        AbstractEstimator.__init__(self)

        self.memory_length = None
        self.parameters_shape = None

    ####################################################################################################################
    ### Public methods:
    ####################################################################################################################

    def update(self):
        """
        Runs the scipy optimize routine and updates the statistical model.
        """

        # Initialisation -----------------------------------------------------------------------------------------------
        # First case: we use what's stored in the state file
        if Settings().load_state:
            x0, self.current_iteration, self.parameters_shape = self._load_state_file()
            self._set_parameters(self._unvectorize_parameters(x0))  # Propagate the parameter values.
            print("State file loaded, it was at iteration", self.current_iteration)

        # Second case: we use the native initialisation of the model.
        else:
            parameters = self._get_parameters()
            self.current_iteration = 0

            self.parameters_shape = {key: value.shape for key, value in parameters.items()}
            x0 = self._vectorize_parameters(parameters)

        # Main loop ----------------------------------------------------------------------------------------------------
        print('')

        result = minimize(self._cost_and_derivative, x0.astype('float64'),
                          method='L-BFGS-B', jac=True, callback=self._callback,
                          options={
                              # No idea why the '-2' is necessary.
                              'maxiter': self.max_iterations - 2 - (self.current_iteration - 1),
                              'ftol': self.convergence_tolerance,
                              # Number of previous gradients used to approximate the Hessian.
                              'maxcor': self.memory_length,
                              'disp': True,
                          })

        # Finalization -------------------------------------------------------------------------------------------------
        self._set_parameters(self._unvectorize_parameters(result.x))  # Probably already done in _callback.

        print('>> Write output files ...')
        self.write()
        print('>> Done.')

    def write(self):
        """
        Save the results.
        """
        self.statistical_model.write(self.dataset, self.population_RER, self.individual_RER)

    ####################################################################################################################
    ### Private methods:
    ####################################################################################################################

    def _cost_and_derivative(self, x):
        # Propagates the parameter value to all necessary attributes.
        self._set_parameters(self._unvectorize_parameters(x))

        # Call the model method.
        attachment, regularity, gradient = self.statistical_model.compute_log_likelihood(
            self.dataset, self.population_RER, self.individual_RER, with_grad=True)

        # Prepare the outputs: notably linearize and concatenates the gradient.
        cost = - attachment - regularity
        gradient = - np.concatenate([value.flatten() for value in gradient.values()])

        return cost.astype('float64'), gradient.astype('float64')

    def _callback(self, x):
        # Propagate the parameters to all necessary attributes.
        self._set_parameters(self._unvectorize_parameters(x))

        # Save the current statistical model.
        if not (self.current_iteration % self.save_every_n_iters): self.write()

        # Save the state.
        if self.current_iteration % self.save_every_n_iters == 0: self._dump_state_file(x)

        self.current_iteration += 1

    def _get_parameters(self):
        """
        Return a dictionary of numpy arrays.
        """
        out = self.statistical_model.get_fixed_effects()
        out.update(self.population_RER)
        out.update(self.individual_RER)
        assert len(out) == len(self.statistical_model.get_fixed_effects()) \
                           + len(self.population_RER) + len(self.individual_RER)
        return out

    def _vectorize_parameters(self, parameters):
        """
        Returns a 1D numpy array from a dictionary of numpy arrays.
        """
        return np.concatenate([value.flatten() for value in parameters.values()])

    def _unvectorize_parameters(self, x):
        """
        Recover the structure of the parameters
        """
        parameters = {}
        cursor = 0
        for key, shape in self.parameters_shape.items():
            length = np.prod(shape)
            parameters[key] = x[cursor:cursor + length].reshape(shape)
            cursor += length
        return parameters

    def _set_parameters(self, parameters):
        """
        Updates the model and the random effect realization attributes.
        """
        fixed_effects = {key: parameters[key] for key in self.statistical_model.get_fixed_effects().keys()}
        self.statistical_model.set_fixed_effects(fixed_effects)
        self.population_RER = {key: parameters[key] for key in self.population_RER.keys()}
        self.individual_RER = {key: parameters[key] for key in self.individual_RER.keys()}

    def _load_state_file(self):
        """
        loads Settings().state_file and returns what's necessary to restart the scipy optimization.
        """
        d = pickle.load(open(Settings().state_file, 'rb'))
        return d['parameters'], d['current_iteration'], d['parameters_shape']

    def _dump_state_file(self, parameters):
        """
        Dumps the state file with the new value of $x_0$ as argument.
        """
        d = {'parameters': parameters, 'current_iteration': self.current_iteration,
             'parameters_shape': self.parameters_shape}
        pickle.dump(d, open(Settings().state_file, 'wb'))
