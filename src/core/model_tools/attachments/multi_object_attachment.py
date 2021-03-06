import numpy as np
import torch
from torch.autograd import Variable

from support.utilities.general_settings import Settings


class MultiObjectAttachment:
    ####################################################################################################################
    ### Constructor:
    ####################################################################################################################

    def __init__(self):
        # List of strings, e.g. 'varifold' or 'current'.
        self.attachment_types = []

        # List of kernel objects.
        self.kernels = []

    ####################################################################################################################
    ### Public methods:
    ####################################################################################################################

    def compute_weighted_distance(self, data, multi_obj1, multi_obj2, inverse_weights):
        """
        Takes two multiobjects and their new point positions to compute the distances
        """
        distances = self.compute_distances(data, multi_obj1, multi_obj2)
        assert distances.size()[0] == len(inverse_weights)
        inverse_weights_torch = Variable(torch.from_numpy(np.array(
            inverse_weights)).type(Settings().tensor_scalar_type), requires_grad=False)
        return torch.sum(distances / inverse_weights_torch)

    def compute_distances(self, data, multi_obj1, multi_obj2):
        """
        Takes two multiobjects and their new point positions to compute the distances.
        """
        assert len(multi_obj1.object_list) == len(multi_obj2.object_list), \
            "Cannot compute distance between multi-objects which have different number of objects"
        distances = Variable(torch.zeros((len(multi_obj1.object_list),)).type(Settings().tensor_scalar_type),
                             requires_grad=False)

        pos = 0
        for i, obj1 in enumerate(multi_obj1.object_list):
            obj2 = multi_obj2.object_list[i]

            if self.attachment_types[i].lower() == 'current':
                distances[i] = self._current_distance(
                    data['landmark_points'][pos:pos + obj1.get_number_of_points()], obj1, obj2, self.kernels[i])
                pos += obj1.get_number_of_points()

            elif self.attachment_types[i].lower() == 'varifold':
                distances[i] = self._varifold_distance(
                    data['landmark_points'][pos:pos + obj1.get_number_of_points()], obj1, obj2, self.kernels[i])
                pos += obj1.get_number_of_points()

            elif self.attachment_types[i].lower() == 'landmark':
                distances[i] = self._landmark_distance(
                    data['landmark_points'][pos:pos + obj1.get_number_of_points()], obj2)
                pos += obj1.get_number_of_points()

            elif self.attachment_types[i].lower() == 'l2':
                assert obj1.type.lower() == 'image' and obj2.type.lower() == 'image'
                distances[i] = self._L2_distance(data['image_intensities'], obj2)

            else:
                assert False, "Please implement the distance {e} you are trying to use :)".format(
                    e=self.attachment_types[i])

        return distances

    ####################################################################################################################
    ### Private methods:
    ####################################################################################################################

    def _current_distance(self, points, source, target, kernel):
        """
        Compute the current distance between source and target, assuming points are the new points of the source
        We assume here that the target never moves.
        """
        assert kernel.kernel_width > 0, "Please set the kernel width in current_distance computation"

        c1, n1 = source.get_centers_and_normals(points)
        c2, n2 = target.get_centers_and_normals()

        def current_scalar_product(points_1, points_2, normals_1, normals_2):
            return torch.dot(normals_1.view(-1), kernel.convolve(points_1, points_2, normals_2).view(-1))

        if target.norm is None:
            target.norm = current_scalar_product(c2, c2, n2, n2)

        return current_scalar_product(c1, c1, n1, n1) + target.norm - 2 * current_scalar_product(c1, c2, n1, n2)

    def _varifold_distance(self, points, source, target, kernel):

        """
        Returns the current distance between the 3D meshes
        source and target are SurfaceMesh objects
        points are source points (torch tensor)
        """
        c1, n1 = source.get_centers_and_normals(points)
        c2, n2 = target.get_centers_and_normals()

        # alpha = normales non unitaires
        areaa = torch.norm(n1, 2, 1)
        areab = torch.norm(n2, 2, 1)

        nalpha = n1 / areaa.unsqueeze(1)
        nbeta = n2 / areab.unsqueeze(1)

        # def gaussian(r2, s):
        #     return torch.exp(-r2 / (s * s))
        #
        # def binet(prs):
        #     return prs ** 2
        #
        # def squdistance_matrix(ax, by):
        #     return torch.sum((ax.unsqueeze(1) - by.unsqueeze(0)) ** 2, 2)

        # def varifold_scalar_product(x, y, areaa, areab, nalpha, nbeta):
        #     return torch.sum(torch.sum(
        #         areaa.unsqueeze(1) * areab.unsqueeze(0)
        #         * gaussian(squdistance_matrix(x, y), kernel_width)
        #         * binet(torch.mm(nalpha, torch.t(nbeta))), 1), 0)

        def varifold_scalar_product(x, y, areaa, areab, nalpha, nbeta):
            return torch.dot(areaa.view(-1), kernel.convolve((x, nalpha), (y, nbeta), areab.view(-1, 1),
                                                             mode='varifold').view(-1))

        if target.norm is None:
            target.norm = varifold_scalar_product(c2, c2, areab, areab, nbeta, nbeta)

        return varifold_scalar_product(c1, c1, areaa, areaa, nalpha, nalpha) + target.norm \
               - 2 * varifold_scalar_product(c1, c2, areaa, areab, nalpha, nbeta)

    def _landmark_distance(self, points, target):
        """
        Point correspondance distance
        """
        target_points = target.get_points_torch()
        return torch.sum((points.view(-1) - target_points.view(-1)) ** 2)

    def _L2_distance(self, intensities, target):
        """
        L2 image distance.
        """
        target_intensities = target.get_intensities_torch()
        return torch.sum((intensities.view(-1) - target_intensities.view(-1)) ** 2)

