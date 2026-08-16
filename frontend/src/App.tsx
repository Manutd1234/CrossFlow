import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { identityKey, isDriverView } from './utils/access';
import { Header } from './components/app-shell/Header';
import type { AppTab } from './components/app-shell/Navigation';

import {
  AuthSession, AuthStatus, Corridor, CorridorRoute, DataSource, FerrySchedule, Fetched, LiveTrafficData,
  FerryRefreshReport, FerryTimetableMetadata, OperationsSummary, PortStatus, Provenance,
  RouteLocation, RouteOptimizationResult, StoredSession, VehicleType,
} from './types';
import {
  fetchAuthStatus, fetchSession, readStoredSession, supabaseConfigured, validSession,
} from './services/auth';
import {
  fetchCorridorRoutes, fetchCorridors, fetchFerries, fetchOperationsSummary,
  fetchLiveTraffic, fetchPortIntelligence, fetchRouteLocations,
  refreshOfficialFerrySources, scheduleInformedPortSeed,
} from './services/api';
import {
  INITIAL_CORRIDORS, INITIAL_FERRIES, MOCK_OPERATIONS,
  PUBLISHED_FERRY_TIMETABLE_METADATA, ROUTE_LOCATIONS,
} from './data/mockData';

const POLL_INTERVAL_MS = 30_000;
const DRIVER_TABS: readonly AppTab[] = ['map', 'route'];

const MapView = lazy(() => import('./components/corridor-map/MapView').then(module => ({ default: module.MapView })));
const RouteOptimizer = lazy(() => import('./components/route-planner/RouteOptimizer').then(module => ({ default: module.RouteOptimizer })));
const SignInPanel = lazy(() => import('./components/auth/SignInPanel').then(module => ({ default: module.SignInPanel })));
const FerryPortTracker = lazy(() => import('./components/ferry-port/FerryPortTracker').then(module => ({ default: module.FerryPortTracker })));
const OperationsAnalytics = lazy(() => import('./components/operations/OperationsAnalytics').then(module => ({ default: module.OperationsAnalytics })));

function LoadingPanel() {
  return (
    <div className="glass-panel app-loading-panel" role="status" aria-live="polite">
      <span className="app-loading-spinner" aria-hidden="true" />
      <span>Loading workspace…</span>
    </div>
  );
}

const VIEW_META: Record<AppTab, { title: string; description: string }> = {
  map: {
    title: 'Batam Congestion Watch',
    description: 'Scan 30 photo-backed planning hotspots and move directly from an area to route planning.',
  },
  route: {
    title: 'Cross-Border Route Planner',
    description: 'Compose local roads, a published ferry crossing, and onward roads between Singapore and Batam.',
  },
  analytics: {
    title: 'Ferry, Port & Operations Analytics',
    description: 'Track terminal activity and review network performance, bottlenecks, and decision-support indicators.',
  },
};

export function App() {
  const [activeTab, setActiveTab] = useState<AppTab>('map');

  const [isSignInOpen, setIsSignInOpen] = useState<boolean>(false);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [session, setSession] = useState<StoredSession | null>(() => readStoredSession());
  const [identity, setIdentity] = useState<AuthSession | null>(null);
  const [authResolved, setAuthResolved] = useState(false);
  // Remembered for the tab only: a guest should not be re-prompted on every
  // reload, but a new visit starts at the sign-in screen again.
  const [isGuest, setIsGuest] = useState<boolean>(
    () => window.sessionStorage?.getItem('crossflow.guest') === '1',
  );
  const [routes, setRoutes] = useState<CorridorRoute[]>([]);
  const [routesLoading, setRoutesLoading] = useState(true);
  const [corridors, setCorridors] = useState<Corridor[]>(INITIAL_CORRIDORS);
  const [ferries, setFerries] = useState<FerrySchedule[]>(INITIAL_FERRIES);
  const [ferryTimetable, setFerryTimetable] = useState<FerryTimetableMetadata>(
    PUBLISHED_FERRY_TIMETABLE_METADATA,
  );
  const [ports, setPorts] = useState<PortStatus[]>(() => scheduleInformedPortSeed());
  const [portSource, setPortSource] = useState<DataSource>('offline');
  const [portsLoading, setPortsLoading] = useState(true);
  const [portsError, setPortsError] = useState<string | null>(null);
  const [isFerryRefreshing, setIsFerryRefreshing] = useState(false);
  const [operationsSnapshot, setOperationsSnapshot] = useState<Fetched<OperationsSummary> | null>(null);
  const [routeLocations, setRouteLocations] = useState<RouteLocation[]>(ROUTE_LOCATIONS);
  const [trafficSnapshot, setTrafficSnapshot] = useState<Fetched<LiveTrafficData> | null>(null);

  const [dataSource, setDataSource] = useState<DataSource>('offline');
  const [ferrySource, setFerrySource] = useState<DataSource>('offline');
  const [provenance, setProvenance] = useState<Provenance | undefined>();
  const [lastUpdated, setLastUpdated] = useState<string>('');

  const [routeOriginId, setRouteOriginId] = useState<string>('mukakuning');
  const [routeDestinationId, setRouteDestinationId] = useState<string>('batam_centre');

  // Solver state lives here rather than inside RouteOptimizer: that component is
  // conditionally rendered, so switching tabs used to unmount it and silently
  // discard a computed route.
  const [routeResult, setRouteResult] = useState<RouteOptimizationResult | null>(null);
  const [routeSource, setRouteSource] = useState<DataSource>('simulated');
  const [vehicleType, setVehicleType] = useState<VehicleType>('CARGO_TRUCK');
  const [weather, setWeather] = useState<number>(0);
  const [departureHour, setDepartureHour] = useState<number>(() => new Date().getHours());
  const ferryRefreshInFlightRef = useRef(false);
  const ferryRequestEpochRef = useRef(0);

  const loadData = useCallback(async () => {
    // Parallel, not sequential: a slow provider must not delay unrelated
    // ferry, operations, and corridor requests one after another.
    const ferryRequestEpoch = ferryRequestEpochRef.current;
    const ferryRequest = ferryRefreshInFlightRef.current
      ? Promise.resolve(null)
      : fetchFerries();
    const [c, f, o, traffic] = await Promise.all([
      fetchCorridors(), ferryRequest, fetchOperationsSummary(), fetchLiveTraffic(),
    ]);

    setCorridors(c.data);
    // A poll that began before a manual official-source refresh must not write
    // its older response over the coordinated refresh result.
    if (f
      && ferryRequestEpoch === ferryRequestEpochRef.current
      && !ferryRefreshInFlightRef.current) {
      setFerries(f.data);
      setFerryTimetable(f.timetable);
      setFerrySource(f.source);
    }
    setOperationsSnapshot(o);
    setTrafficSnapshot(traffic);
    // The global badge describes road traffic. Ferry schedules and operations
    // identify their own modelled/offline state inside their respective views.
    // This also lets a configured live traffic provider surface as live even
    // while the ferry timetable remains modelled.
    const roadSource = traffic.source === 'offline' ? c.source : traffic.source;
    setDataSource(roadSource);
    setProvenance(traffic.provenance ?? c.provenance ?? o.provenance);
    setLastUpdated(traffic.source === 'offline' ? c.fetchedAt : traffic.fetchedAt);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      await loadData();
      if (!cancelled) timer = setTimeout(poll, POLL_INTERVAL_MS);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loadData]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const pollPorts = async () => {
      const requestEpoch = ferryRequestEpochRef.current;
      if (!ferryRefreshInFlightRef.current) {
        try {
          const response = await fetchPortIntelligence();
          if (!cancelled
            && requestEpoch === ferryRequestEpochRef.current
            && !ferryRefreshInFlightRef.current) {
            setPorts(response.data);
            setPortSource(response.source);
            setPortsError(null);
          }
        } catch (error) {
          if (!cancelled && requestEpoch === ferryRequestEpochRef.current) {
            setPortsError(error instanceof Error
              ? error.message
              : 'Port intelligence could not be refreshed.');
          }
        } finally {
          if (!cancelled && requestEpoch === ferryRequestEpochRef.current) {
            setPortsLoading(false);
          }
        }
      }
      if (!cancelled) timer = setTimeout(pollPorts, 10_000);
    };

    void pollPorts();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // Road geometry and the route-place catalogue are both static, so fetch each
  // once. The corridor geometry is what lets the map draw the corridor the
  // Corridor tab is reporting congestion for.
  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchCorridorRoutes(), fetchRouteLocations()])
      .then(([corridorRoutes, locations]) => {
        if (cancelled) return;
        setRoutes(corridorRoutes.data);
        setRouteLocations(locations.data);
      })
      .finally(() => {
        if (!cancelled) setRoutesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Restore a stored session on load and ask the server who it belongs to.
   *
   * Every failure here is silent by design: auth is optional, so an expired
   * token or an unreachable Supabase must leave the public corridor, ferry and
   * analytics views exactly as usable as they are signed out.
   */
  useEffect(() => {
    let cancelled = false;
    fetchAuthStatus().then(status => {
      if (!cancelled) setAuthStatus(status);
    });

    const stored = readStoredSession();
    // Always resolve through a promise so the gate's readiness flag is never
    // set synchronously inside the effect, which would cascade renders.
    Promise.resolve()
      .then(async () => {
        if (!stored) return;
        const fresh = await validSession(stored);
        const resolved = await fetchSession(fresh);
        if (cancelled) return;
        setSession(fresh);
        setIdentity(resolved);
      })
      .catch(() => {
        if (cancelled) return;
        setSession(null);
        setIdentity(null);
      })
      .finally(() => {
        if (!cancelled) setAuthResolved(true);
      });
    return () => { cancelled = true; };
  }, []);

  // Offering sign-in needs both halves configured: the browser needs the
  // Supabase project to authenticate against, and the API needs to be able to
  // resolve a role from it.
  const signInAvailable = supabaseConfigured()
    && (authStatus === null || authStatus.enabled);
  const driverAccess = isDriverView(identity, isGuest);

  const visibleActiveTab: AppTab = driverAccess && activeTab === 'analytics'
    ? 'map'
    : activeTab;

  // Changing who is signed in — including signing out — must not leave the
  // previous account's route on screen. Undefined marks the first run, so
  // restoring a stored session on load is not mistaken for a switch.
  const identityKeyRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const key = identityKey(identity);
    if (identityKeyRef.current === undefined) {
      identityKeyRef.current = key;
      return;
    }
    if (identityKeyRef.current === key) return;
    identityKeyRef.current = key;
    setRouteResult(null);
    setActiveTab('map');
  }, [identity]);

  // A resolved account supersedes the remembered guest flag outright, so clear
  // it rather than leaving two disagreeing sources of truth behind. Signing in
  // also dismisses the panel: leaving it open parked a "Signed in" dialog on
  // top of the workspace, which reads as though sign-in had not finished.
  useEffect(() => {
    if (!identity) return;
    window.sessionStorage?.removeItem('crossflow.guest');
    setIsGuest(false);
    setIsSignInOpen(false);
  }, [identity]);

  // Entering or leaving the driver view must start from a clean pane. The
  // driver's only route is the one they load by code, so a journey left over
  // from planning would otherwise appear under a different code than the one
  // in the lookup box.
  const driverAccessRef = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    if (driverAccessRef.current === undefined) {
      driverAccessRef.current = driverAccess;
      return;
    }
    if (driverAccessRef.current === driverAccess) return;
    driverAccessRef.current = driverAccess;
    setRouteResult(null);
    setActiveTab('map');
  }, [driverAccess]);

  const continueAsGuest = useCallback(() => {
    window.sessionStorage?.setItem('crossflow.guest', '1');
    setIsGuest(true);
  }, []);

  // Show the sign-in screen first. Waiting for authResolved avoids flashing it
  // at someone whose stored session is about to restore.
  const showAuthGate = signInAvailable && authResolved && !identity && !isGuest;

  const handleSelectCorridorAndSolve = useCallback((corridorId: string) => {
    const corridor = corridors.find(item => item.id === corridorId);
    if (corridor?.origin && corridor.destination) {
      setRouteOriginId(corridor.origin);
      setRouteDestinationId(corridor.destination);
      setRouteResult(null);
    }
    setActiveTab('route');
  }, [corridors]);

  const handleRefreshOfficialFerrySources = useCallback(async (): Promise<FerryRefreshReport> => {
    if (ferryRefreshInFlightRef.current) {
      throw new Error('An official-source refresh is already in progress.');
    }

    ferryRefreshInFlightRef.current = true;
    ferryRequestEpochRef.current += 1;
    setIsFerryRefreshing(true);
    setPortsLoading(true);
    setPortsError(null);
    try {
      const response = await refreshOfficialFerrySources();
      setFerries(response.data);
      setFerryTimetable(response.timetable);
      setFerrySource(response.source);
      setPorts(response.ports);
      setPortSource(response.source);
      return response.refresh;
    } finally {
      ferryRefreshInFlightRef.current = false;
      setIsFerryRefreshing(false);
      setPortsLoading(false);
    }
  }, []);

  const activeView = VIEW_META[visibleActiveTab];
  const operations = operationsSnapshot?.data ?? MOCK_OPERATIONS;

  return (
    <>
      {showAuthGate ? (
        <Suspense fallback={<LoadingPanel />}>
          <SignInPanel
            variant="page"
            status={authStatus}
            session={session}
            identity={identity}
            onSessionChange={setSession}
            onIdentityChange={setIdentity}
            onClose={continueAsGuest}
            onContinueAsGuest={continueAsGuest}
          />
        </Suspense>
      ) : null}

      <a className="app-skip-link" href="#main-content">Skip to main content</a>

      <Header
        onOpenSignIn={() => setIsSignInOpen(true)}
        identity={identity}
        signInAvailable={signInAvailable}
        activeTab={visibleActiveTab}
        setActiveTab={setActiveTab}
        allowedTabs={driverAccess ? DRIVER_TABS : undefined}
        dataSource={dataSource}
        provenance={provenance}
        lastUpdated={lastUpdated}
      />

      <div className="app-workspace">
        <main
          id="main-content"
          className="app-main"
          role="tabpanel"
          aria-labelledby={`workspace-tab-${visibleActiveTab}`}
          tabIndex={-1}
        >
          <div className="app-view-heading">
            <div>
              <h2 id="active-view-title">{activeView.title}</h2>
              <span>{activeView.description}</span>
            </div>
            <span className="app-track-label">Batam · Riau Islands</span>
          </div>

          <section className="app-view-content" aria-labelledby="active-view-title">
            <Suspense fallback={<LoadingPanel />}>
              {visibleActiveTab === 'map' && (
                <MapView
                  corridors={corridors}
                  routes={routes}
                  routesLoading={routesLoading}
                  trafficSnapshot={trafficSnapshot}
                  onSelectCorridor={handleSelectCorridorAndSolve}
                />
              )}

              {visibleActiveTab === 'route' && (
                <RouteOptimizer
                  locations={routeLocations}
                  originId={routeOriginId}
                  setOriginId={setRouteOriginId}
                  destinationId={routeDestinationId}
                  setDestinationId={setRouteDestinationId}
                  result={routeResult}
                  setResult={setRouteResult}
                  resultSource={routeSource}
                  setResultSource={setRouteSource}
                  vehicleType={vehicleType}
                  setVehicleType={setVehicleType}
                  weather={weather}
                  setWeather={setWeather}
                  hour={departureHour}
                  setHour={setDepartureHour}
                  driverAccess={driverAccess}
                  accessToken={session?.accessToken ?? null}
                />
              )}

              {visibleActiveTab === 'analytics' && (
                <div className="analytics-workspace-stack">
                  <OperationsAnalytics
                    operations={operations}
                    operationsSnapshot={operationsSnapshot}
                    corridors={corridors}
                    ferryAndPortsContent={(
                      <FerryPortTracker
                        ferries={ferries}
                        dataSource={ferrySource}
                        timetable={ferryTimetable}
                        ports={ports}
                        portSource={portSource}
                        portsLoading={portsLoading}
                        portsError={portsError}
                        isRefreshingOfficialSources={isFerryRefreshing}
                        onRefreshOfficialSources={handleRefreshOfficialFerrySources}
                      />
                    )}
                  />
                </div>
              )}
            </Suspense>
          </section>
        </main>
      </div>

      <Suspense fallback={null}>
        {isSignInOpen && (
          <SignInPanel
            status={authStatus}
            session={session}
            identity={identity}
            onSessionChange={setSession}
            onIdentityChange={setIdentity}
            onClose={() => setIsSignInOpen(false)}
          />
        )}
      </Suspense>
    </>
  );
}

export default App;
