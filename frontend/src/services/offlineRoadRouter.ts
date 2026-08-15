import type { OfflineRoadPlan, OfflineRoutingContext } from '../workers/offlineRouterCore';

const graphUrl = `${import.meta.env.BASE_URL}assets/batam-road-graph.json`;

interface WorkerResponse {
  id: number;
  plan?: OfflineRoadPlan;
  error?: string;
}

interface PendingRequest {
  resolve: (plan: OfflineRoadPlan) => void;
  reject: (error: Error) => void;
  timeout: number;
}

let worker: Worker | null = null;
let requestSequence = 0;
const pending = new Map<number, PendingRequest>();

function sharedWorker(): Worker | null {
  if (typeof Worker === 'undefined') return null;
  if (worker) return worker;
  worker = new Worker(new URL('../workers/offlineRouter.worker.ts', import.meta.url), {
    type: 'module',
    name: 'crossflow-offline-road-router',
  });
  worker.addEventListener('message', (event: MessageEvent<WorkerResponse>) => {
    const request = pending.get(event.data.id);
    if (!request) return;
    window.clearTimeout(request.timeout);
    pending.delete(event.data.id);
    if (event.data.plan) request.resolve(event.data.plan);
    else request.reject(new Error(event.data.error ?? 'Offline road routing failed.'));
  });
  worker.addEventListener('error', () => {
    pending.forEach((request) => {
      window.clearTimeout(request.timeout);
      request.reject(new Error('The offline road routing worker stopped unexpectedly.'));
    });
    pending.clear();
    worker?.terminate();
    worker = null;
  });
  return worker;
}

export function planWithBundledRoadGraph(
  origin: [number, number],
  destination: [number, number],
  destinationName: string,
  context: OfflineRoutingContext,
): Promise<OfflineRoadPlan> {
  const routerWorker = sharedWorker();
  if (!routerWorker) return Promise.reject(new Error('Web Workers are unavailable.'));
  const id = ++requestSequence;
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pending.delete(id);
      reject(new Error('The offline road graph took too long to calculate a route.'));
    }, 15_000);
    pending.set(id, { resolve, reject, timeout });
    routerWorker.postMessage({ id, graphUrl, origin, destination, destinationName, context });
  });
}
