import os.path
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + '../../../../../')

import torch
from torch.autograd import Variable
import numpy as np
import warnings

from pydeformetrica.src.in_out.utils import *
from pydeformetrica.src.support.utilities.general_settings import Settings
from pydeformetrica.src.core.model_tools.deformations.exponential import Exponential


class Geodesic:
    """
    Control-point-based LDDMM geodesic.
    See "Morphometry of anatomical shape complexes with dense deformations and sparse parameters",
    Durrleman et al. (2013).

    """

    ####################################################################################################################
    ### Constructor:
    ####################################################################################################################

    def __init__(self):

        self.concentration_of_time_points = None

        self.t0 = None
        self.tmax = None
        self.tmin = None

        self.control_points_t0 = None
        self.momenta_t0 = None
        self.template_data_t0 = None

        self.backward_exponential = Exponential()
        self.forward_exponential = Exponential()

        # Flags to save extra computations that have already been made in the update methods.
        self.shoot_is_modified = True
        self.flow_is_modified = True

    ####################################################################################################################
    ### Encapsulation methods:
    ####################################################################################################################

    def set_use_rk2(self, use_rk2):
        self.backward_exponential.set_use_rk2(use_rk2)
        self.forward_exponential.set_use_rk2(use_rk2)

    def set_kernel(self, kernel):
        self.backward_exponential.kernel = kernel
        self.forward_exponential.kernel = kernel

    def set_t0(self, t0):
        self.t0 = t0
        self.shoot_is_modified = True

    def set_tmin(self, tmin):
        self.tmin = tmin
        self.shoot_is_modified = True

    def set_tmax(self, tmax):
        self.tmax = tmax
        self.shoot_is_modified = True

    def set_template_data_t0(self, td):
        self.template_data_t0 = td
        self.flow_is_modified = True

    def set_control_points_t0(self, cp):
        self.control_points_t0 = cp
        self.shoot_is_modified = True

    def set_momenta_t0(self, mom):
        self.momenta_t0 = mom
        self.shoot_is_modified = True

    def get_template_data(self, time, with_index=False):
        """
        Returns the position of the landmark points, at the given time.
        """
        assert self.tmin <= time.data.numpy()[0] <= self.tmax
        if self.shoot_is_modified or self.flow_is_modified:
            msg = "Asking for deformed template data but the geodesic was modified and not updated"
            warnings.warn(msg)

        times = Variable(torch.from_numpy(np.asarray(self._get_times())).type(Settings().tensor_scalar_type),
                         requires_grad=False)
        _, index = torch.min((times - time) ** 2, 0)

        # if time.requires_grad:
        #     index.backward()
        #     print('hello')

        if with_index: return torch.stack(self._get_template_trajectory())[index].squeeze(), index
        else: return torch.stack(self._get_template_trajectory())[index].squeeze()

    ####################################################################################################################
    ### Main methods:
    ####################################################################################################################

    def update(self):
        """
        Compute the time bounds, accordingly sets the number of points and momenta of the attribute exponentials,
        then shoot and flow them.
        """

        assert self.t0 >= self.tmin, "tmin should be smaller than t0"
        assert self.t0 <= self.tmax, "tmax should be larger than t0"

        # Backward exponential -----------------------------------------------------------------------------------------
        delta_t = self.t0 - self.tmin
        self.backward_exponential.number_of_time_points = max(1, int(delta_t * self.concentration_of_time_points + 1.5))
        if self.shoot_is_modified:
            self.backward_exponential.set_initial_momenta(- self.momenta_t0 * delta_t)
            self.backward_exponential.set_initial_control_points(self.control_points_t0)
        if self.flow_is_modified:
            self.backward_exponential.set_initial_template_data(self.template_data_t0)
        if self.backward_exponential.number_of_time_points > 1:
            self.backward_exponential.update()

        # Forward exponential ------------------------------------------------------------------------------------------
        delta_t = self.tmax - self.t0
        self.forward_exponential.number_of_time_points = max(1, int(delta_t * self.concentration_of_time_points + 1.5))
        if self.shoot_is_modified:
            self.forward_exponential.set_initial_momenta(self.momenta_t0 * delta_t)
            self.forward_exponential.set_initial_control_points(self.control_points_t0)
        if self.flow_is_modified:
            self.forward_exponential.set_initial_template_data(self.template_data_t0)
        if self.forward_exponential.number_of_time_points > 1:
            self.forward_exponential.update()

        self.shoot_is_modified = False
        self.flow_is_modified = False

    def get_norm_squared(self):
        """
        Get the norm of the geodesic.
        """
        return self.forward_exponential.get_norm_squared()

    def parallel_transport(self, momenta_to_transport_t0, with_tangential_component=True):
        """
        :param momenta_to_transport_t0: the vector to parallel transport, given at t0 and carried at control_points_t0
        :returns: the full trajectory of the parallel transport, from tmin to tmax
        """

        if self.shoot_is_modified:
            msg = "Trying to parallel transport but the geodesic object was modified, please update before."
            warnings.warn(msg)

        if self.backward_exponential.number_of_time_points > 1:
            backward_transport = self.backward_exponential.parallel_transport(momenta_to_transport_t0,
                                                                              with_tangential_component)
        else:
            backward_transport = []

        if self.forward_exponential.number_of_time_points > 1:
            forward_transport = self.forward_exponential.parallel_transport(momenta_to_transport_t0,
                                                                            with_tangential_component)
        else:
            forward_transport = []

        return backward_transport[::-1] + forward_transport[1:]

    ####################################################################################################################
    ### Private methods:
    ####################################################################################################################

    def _get_times(self):
        times_backward = []
        if self.backward_exponential.number_of_time_points > 1:
            times_backward = np.linspace(
                self.t0, self.tmin, num=self.backward_exponential.number_of_time_points).tolist()

        times_forward = []
        if self.forward_exponential.number_of_time_points > 1:
            times_forward = np.linspace(
                self.t0, self.tmax, num=self.forward_exponential.number_of_time_points).tolist()

        return times_backward[::-1] + times_forward[1:]

    def _get_control_points_trajectory(self):
        if self.shoot_is_modified:
            msg = "Trying to get cp trajectory in a non updated geodesic."
            warnings.warn(msg)

        backward_control_points_t = []
        if self.backward_exponential.number_of_time_points > 1:
            backward_control_points_t = self.backward_exponential.control_points_t

        forward_control_points_t = []
        if self.forward_exponential.number_of_time_points > 1:
            forward_control_points_t = self.forward_exponential.control_points_t

        return backward_control_points_t[::-1] + forward_control_points_t[1:]

    def _get_momenta_trajectory(self):
        if self.shoot_is_modified:
            msg = "Trying to get mom trajectory in non updated geodesic."
            warnings.warn(msg)

        backward_momenta_t = []
        if self.backward_exponential.number_of_time_points > 1:
            backward_length = self.t0 - self.tmin
            backward_momenta_t = self.backward_exponential.momenta_t
            backward_momenta_t = [elt / backward_length for elt in backward_momenta_t]

        forward_momenta_t = []
        if self.forward_exponential.number_of_time_points > 1:
            forward_length = self.tmax - self.t0
            forward_momenta_t = self.forward_exponential.momenta_t
            forward_momenta_t = [elt / forward_length for elt in forward_momenta_t]

        return backward_momenta_t[::-1] + forward_momenta_t[1:]

    def _get_template_trajectory(self):
        if self.shoot_is_modified or self.flow_is_modified:
            msg = "Trying to get mom trajectory in non updated geodesic."
            warnings.warn(msg)

        backward_template_t = []
        if self.backward_exponential.number_of_time_points > 1:
            backward_template_t = self.backward_exponential.template_data_t

        forward_template_t = []
        if self.forward_exponential.number_of_time_points > 1:
            forward_template_t = self.forward_exponential.template_data_t

        return backward_template_t[::-1] + forward_template_t[1:]

    ####################################################################################################################
    ### Writing methods:
    ####################################################################################################################

    def write(self, root_name, objects_name, objects_extension, template):

        # Initialization -----------------------------------------------------------------------------------------------
        template_data_memory = template.get_points()

        # Core loop ----------------------------------------------------------------------------------------------------
        times = self._get_times()
        template_data_t = self._get_template_trajectory()

        for t, (time, template_data) in enumerate(zip(times, template_data_t)):
            names = []
            for k, (object_name, object_extension) in enumerate(zip(objects_name, objects_extension)):
                name = root_name + '__GeodesicFlow__' + object_name + '__tp_' + str(t) + ('__age_%.2f' % time) + object_extension
                names.append(name)
            template.set_data(template_data.data.numpy())
            template.write(names)

        # Finalization -------------------------------------------------------------------------------------------------
        template.set_data(template_data_memory)