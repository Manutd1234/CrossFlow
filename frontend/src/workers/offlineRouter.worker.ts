/// <reference lib="webworker" />

import {
  planOfflineRoadRoutes,
  type OfflineGraph,
  type OfflineRoutingContext,
} from './offlineRouterCore';

interface RouteWorkerRequest {
  id: number;
  graphUrl: string;
  origin: [number, number];
  destination: [number, number];
  destinationName: string;
  context: OfflineRoutingContext;
}

let graphPromise: Promise<OfflineGraph> | null = null;

function loadGraph(url: string): Promise<OfflineGraph> {
  if (!graphPromise) {
    graphPromise = fetch(url).then(async (response) => {
      if (!response.ok) throw new Error(`Offline road graph failed to load (${response.status}).`);
      return response.json() as Promise<OfflineGraph>;
    }).catch((error: unknown) => {
      // A transient asset/network failure must not poison every future Plan
      // action for the lifetime of the page.
      graphPromise = null;
      throw error;
    });
  }
  return graphPromise;
}

self.addEventListener('message', (event: MessageEvent<RouteWorkerRequest>) => {
  const request = event.data;
  void loadGraph(request.graphUrl)
    .then((graph) => planOfflineRoadRoutes(
      graph,
      request.origin,
      request.destination,
      request.destinationName,
      3,
      request.context,
    ))
    .then((plan) => self.postMessage({ id: request.id, plan }))
    .catch((error: unknown) => self.postMessage({
      id: request.id,
      error: error instanceof Error ? error.message : 'Offline road routing failed.',
    }));
});

export {};
