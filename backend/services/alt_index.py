"""Directed ALT landmark indexes for the committed road graph.

The module deliberately knows nothing about CrossFlow's ``RoadEdge`` type. It
accepts a directed adjacency of ``(target, nonnegative_distance)`` pairs so
the offline builder and the runtime router can share the same implementation.
Distances are stored as float64 values: rounding a landmark distance upward
could make a supposedly admissible lower bound unsafe.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


ALT_FORMAT_VERSION = 1


def _validate_adjacency(
    adjacency: Mapping[int, Sequence[tuple[int, float]]],
) -> tuple[int, ...]:
    nodes = set(adjacency)
    for source, edges in adjacency.items():
        if not isinstance(source, int):
            raise TypeError("ALT adjacency node IDs must be integers.")
        for target, distance in edges:
            if not isinstance(target, int):
                raise TypeError("ALT adjacency target IDs must be integers.")
            if target not in nodes:
                raise ValueError("ALT adjacency references an unknown target node.")
            value = float(distance)
            if not np.isfinite(value) or value < 0.0:
                raise ValueError("ALT edge distances must be finite and nonnegative.")
    return tuple(sorted(nodes))


def _dijkstra(
    start: int,
    adjacency: Mapping[int, Sequence[tuple[int, float]]],
) -> dict[int, float]:
    distances = {node: float("inf") for node in adjacency}
    distances[start] = 0.0
    queue: list[tuple[float, int]] = [(0.0, start)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        for target, edge_distance in adjacency.get(node, ()):
            candidate = distance + float(edge_distance)
            if candidate < distances[target]:
                distances[target] = candidate
                heapq.heappush(queue, (candidate, target))
    return distances


def _reverse_adjacency(
    nodes: Sequence[int],
    adjacency: Mapping[int, Sequence[tuple[int, float]]],
) -> dict[int, tuple[tuple[int, float], ...]]:
    reverse: dict[int, list[tuple[int, float]]] = {node: [] for node in nodes}
    for source in nodes:
        for target, distance in adjacency.get(source, ()):
            reverse[target].append((source, float(distance)))
    return {node: tuple(edges) for node, edges in reverse.items()}


@dataclass(frozen=True, slots=True)
class AltIndex:
    """A directed landmark distance index for one eligibility topology."""

    graph_revision: str
    topology_id: str
    node_ids: np.ndarray
    landmarks: np.ndarray
    forward_distances: np.ndarray
    reverse_distances: np.ndarray
    format_version: int = ALT_FORMAT_VERSION
    _node_positions: dict[int, int] = field(
        init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if self.format_version != ALT_FORMAT_VERSION:
            raise ValueError("Unsupported ALT index format version.")
        if not self.graph_revision or not self.topology_id:
            raise ValueError("ALT index identity fields cannot be empty.")
        if self.node_ids.ndim != 1 or self.landmarks.ndim != 1:
            raise ValueError("ALT node and landmark tables must be one-dimensional.")
        if self.forward_distances.ndim != 2 or self.reverse_distances.ndim != 2:
            raise ValueError("ALT distance arrays must be two-dimensional.")
        expected_shape = (len(self.landmarks), len(self.node_ids))
        if self.forward_distances.shape != expected_shape:
            raise ValueError("ALT forward distance shape does not match its tables.")
        if self.reverse_distances.shape != expected_shape:
            raise ValueError("ALT reverse distance shape does not match its tables.")
        if len({int(node) for node in self.node_ids.tolist()}) != len(self.node_ids):
            raise ValueError("ALT node IDs must be unique.")
        if len({int(node) for node in self.landmarks.tolist()}) != len(self.landmarks):
            raise ValueError("ALT landmark IDs must be unique.")
        if not np.issubdtype(self.forward_distances.dtype, np.floating):
            raise TypeError("ALT forward distances must use a floating dtype.")
        if not np.issubdtype(self.reverse_distances.dtype, np.floating):
            raise TypeError("ALT reverse distances must use a floating dtype.")
        if np.any(np.isfinite(self.forward_distances) & (self.forward_distances < 0.0)):
            raise ValueError("ALT forward distances cannot be negative.")
        if np.any(np.isfinite(self.reverse_distances) & (self.reverse_distances < 0.0)):
            raise ValueError("ALT reverse distances cannot be negative.")
        object.__setattr__(
            self,
            "_node_positions",
            {int(node): index for index, node in enumerate(self.node_ids.tolist())},
        )

    @classmethod
    def build(
        cls,
        adjacency: Mapping[int, Sequence[tuple[int, float]]],
        landmarks: Sequence[int],
        *,
        graph_revision: str,
        topology_id: str,
    ) -> AltIndex:
        nodes = _validate_adjacency(adjacency)
        if not landmarks:
            raise ValueError("ALT requires at least one landmark.")
        node_set = set(nodes)
        landmark_ids = tuple(int(node) for node in landmarks)
        if len(set(landmark_ids)) != len(landmark_ids):
            raise ValueError("ALT landmarks must be unique.")
        if any(node not in node_set for node in landmark_ids):
            raise ValueError("ALT landmark is not present in the routing graph.")

        reverse = _reverse_adjacency(nodes, adjacency)
        forward_rows = []
        reverse_rows = []
        for landmark in landmark_ids:
            forward = _dijkstra(landmark, adjacency)
            backward = _dijkstra(landmark, reverse)
            forward_rows.append([forward[node] for node in nodes])
            reverse_rows.append([backward[node] for node in nodes])
        return cls(
            graph_revision=graph_revision,
            topology_id=topology_id,
            node_ids=np.asarray(nodes, dtype=np.int64),
            landmarks=np.asarray(landmark_ids, dtype=np.int64),
            forward_distances=np.asarray(forward_rows, dtype=np.float64),
            reverse_distances=np.asarray(reverse_rows, dtype=np.float64),
        )

    def distance_lower_bound(self, node: int, target: int) -> float:
        """Return the directed ALT lower bound in the index's distance units."""
        node_index = self._node_positions.get(int(node))
        target_index = self._node_positions.get(int(target))
        if node_index is None or target_index is None:
            return 0.0

        forward_to_target = self.forward_distances[:, target_index]
        forward_to_node = self.forward_distances[:, node_index]
        reverse_to_node = self.reverse_distances[:, node_index]
        reverse_to_target = self.reverse_distances[:, target_index]
        terms = np.concatenate((
            forward_to_target - forward_to_node,
            reverse_to_node - reverse_to_target,
        ))
        finite_terms = terms[np.isfinite(terms)]
        if finite_terms.size == 0:
            return 0.0
        return max(0.0, float(np.max(finite_terms)))

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata = json.dumps({
            "format_version": self.format_version,
            "graph_revision": self.graph_revision,
            "topology_id": self.topology_id,
        }, sort_keys=True)
        np.savez_compressed(
            destination,
            metadata=np.asarray(metadata),
            node_ids=self.node_ids,
            landmarks=self.landmarks,
            forward_distances=self.forward_distances,
            reverse_distances=self.reverse_distances,
        )

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        expected_graph_revision: str | None = None,
        expected_topology_id: str | None = None,
    ) -> AltIndex:
        with np.load(Path(path), allow_pickle=False) as payload:
            raw_metadata = payload["metadata"]
            metadata = json.loads(str(raw_metadata.item()))
            index = cls(
                graph_revision=str(metadata["graph_revision"]),
                topology_id=str(metadata["topology_id"]),
                node_ids=np.asarray(payload["node_ids"], dtype=np.int64),
                landmarks=np.asarray(payload["landmarks"], dtype=np.int64),
                forward_distances=np.asarray(payload["forward_distances"], dtype=np.float64),
                reverse_distances=np.asarray(payload["reverse_distances"], dtype=np.float64),
                format_version=int(metadata["format_version"]),
            )
        if expected_graph_revision is not None and index.graph_revision != expected_graph_revision:
            raise ValueError("ALT index graph revision does not match the active graph.")
        if expected_topology_id is not None and index.topology_id != expected_topology_id:
            raise ValueError("ALT index topology does not match the active routing view.")
        return index
