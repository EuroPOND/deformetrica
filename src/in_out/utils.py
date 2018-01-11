import os.path
import sys
import numpy as np

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + '../../../')
from pydeformetrica.src.support.utilities.general_settings import Settings
from vtk import vtkPolyDataWriter, vtkPolyData, vtkPoints, vtkDoubleArray


def write_2D_array(array, name):
    """
    Assuming 2-dim array here e.g. control points
    save_name = os.path.join(Settings().output_dir, name)
    np.savetxt(save_name, array)
    """
    save_name = os.path.join(Settings().output_dir, name)
    np.savetxt(save_name, array)


def write_momenta(array, name):
    """
    Saving an array has dim (numsubjects, numcps, dimension), using deformetrica format
    """
    s = array.shape
    if len(s) == 2:
        array = np.array([array])
    save_name = os.path.join(Settings().output_dir, name)
    with open(save_name, "w") as f:
        f.write(str(len(array)) + " " + str(len(array[0])) + " " + str(len(array[0, 0])) + "\n")
        for elt in array:
            f.write("\n")
            for elt1 in elt:
                for elt2 in elt1:
                    f.write(str(elt2) + " ")
                f.write("\n")


def read_momenta(name):
    """
    Loads a file containing momenta, old deformetrica syntax assumed
    """
    try:
        with open(name, "r") as f:
            lines = f.readlines()
            line0 = [int(elt) for elt in lines[0].split()]
            nbSubjects, nbControlPoints, dimension = line0[0], line0[1], line0[2]
            momenta = np.zeros((nbSubjects, nbControlPoints, dimension))
            lines = lines[2:]
            for i in range(nbSubjects):
                for c in range(nbControlPoints):
                    foo = lines[c].split()
                    assert (len(foo) == dimension)
                    foo = [float(elt) for elt in foo]
                    momenta[i, c, :] = foo
                lines = lines[1 + nbControlPoints:]
        if momenta.shape[0] == 1:
            return momenta[0]
        else:
            return momenta

    except ValueError:
        return read_2D_array(name)


def read_2D_array(name):
    """
    Assuming 2-dim array here e.g. control points
    """
    return np.loadtxt(name)


def write_control_points_and_momenta_vtk(control_points, momenta, name):
    """
    Save a file readable by vtk with control points and momenta, for visualization purposes
    """
    nb_cp, dimension = control_points.shape

    #
    # assert control_points.shape == momenta.shape, "Please give momenta " \
    #                                               "and control points of the same shape"
    #
    # figure = mlab.figure(size=(1024, 768), bgcolor=(1, 1, 1))
    #
    # cp_x, cp_y, cp_z = control_points[:, 0], control_points[:, 1], control_points[:, 2]
    # mom_x, mom_y, mom_z = momenta[:, 0], momenta[:, 1], momenta[:, 2]
    #
    # norms = np.array([np.linalg.norm(elt) for elt in momenta])
    #
    # mlab.quiver3d(cp_x, cp_y, cp_z, mom_x, mom_y, mom_z, mode='arrow', scalars = norms/20., resolution=20, figure=figure)
    #
    # mlab.show()


    poly_data = vtkPolyData()
    points = vtkPoints()
    if dimension == 3:
        for i in range(nb_cp):
            points.InsertPoint(i, control_points[i])
    else:
        for i in range(nb_cp):
            points.InsertPoint(i, np.concatenate([control_points[i], [0.]]))

    poly_data.SetPoints(points)

    vectors = vtkDoubleArray()
    vectors.SetNumberOfComponents(dimension)
    for i in range(nb_cp):
        vectors.InsertNextTuple(momenta[i])

    poly_data.GetPointData().SetVectors(vectors)

    save_name = os.path.join(Settings().output_dir, name)

    writer = vtkPolyDataWriter()
    writer.SetInputData(poly_data)
    writer.SetFileName(save_name)
    writer.Update()
