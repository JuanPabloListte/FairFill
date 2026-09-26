import numpy as np

from fleetsim.geo import Route, haversine_km, project_points


def test_haversine_one_degree_latitude():
    assert abs(haversine_km(0, 0, 1, 0) - 111.19) < 0.1


def test_route_resampling_keeps_length_and_endpoints():
    route = Route.from_coords([[-31.0, -64.0], [-31.0, -63.0], [-32.0, -63.0]], duration_h=3, source="test")
    expected = haversine_km(-31, -64, -31, -63) + haversine_km(-31, -63, -32, -63)
    assert abs(route.length_km - expected) < 1e-6
    assert route.position_at(0) == (-31.0, -64.0)
    assert np.allclose(route.position_at(route.length_km), (-32.0, -63.0))


def test_project_points_on_and_off_route():
    route = Route.from_coords([[-31.0, -64.0], [-31.0, -63.0]], duration_h=1, source="test")
    dist, at_km = project_points(route, [-31.0, -31.5], [-63.5, -63.5], max_dist_km=1.0)
    assert dist[0] < 0.2 and abs(at_km[0] - route.length_km / 2) < 0.3
    assert np.isinf(dist[1]) and np.isnan(at_km[1])
