import numpy as np


def thetae(t, q, p):
    """Equivalent potential temperature.

    Parameters
    ----------
    t : Temperature [K]
    q : Specific humidity [kg/kg]
    p : Pressure [hPa]

    Returns
    -------
    theta_e [K]
    """
    Lv = 2.5e6    # J/kg
    Cp = 1005.7   # J/(kg·K)
    Rd = 287.05   # J/(kg·K)

    w = q / (1 - q)
    theta = t * (1000 / p) ** (Rd / Cp)
    return theta * np.exp((Lv * w) / (Cp * t))
