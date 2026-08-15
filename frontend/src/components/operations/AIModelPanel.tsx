import React, { useEffect, useMemo, useState } from 'react';
import {
  Activity, Brain, Database, ExternalLink, RefreshCw, Sparkles, TriangleAlert, Zap,
} from 'lucide-react';

import type { ModelMetricsWithProvenance } from '../../data/modelManifest';
import { fetchModelStatus } from '../../services/api';
import { ICON_SIZE } from '../../theme/iconSizes';
import { DataSource, ModelMetrics } from '../../types';

function formatModelTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat('en-SG', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Singapore',
  }).format(parsed);
}

export const AIModelPanel: React.FC = () => {
  const [metrics, setMetrics] = useState<ModelMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource] = useState<DataSource>('offline');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const loadMetrics = async () => {
      try {
        const res = await fetchModelStatus();
        if (cancelled) return;
        setMetrics(res.data);
        setSource(res.source);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Model telemetry could not be loaded.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void loadMetrics();
    return () => {
      cancelled = true;
    };
  }, []);

  const featureEntries = useMemo(
    () => Object.entries(metrics?.feature_importances ?? {}).sort((a, b) => b[1] - a[1]),
    [metrics?.feature_importances],
  );

  if (!metrics && loading) {
    return (
      <section
        className="glass-panel operations-model-loading"
        role="status"
        aria-live="polite"
        aria-busy="true"
      >
        <RefreshCw className="operations-spinner" aria-hidden="true" size={ICON_SIZE.large} />
        <div>
          <strong className="operations-model-loading__title">
            Connecting to model telemetry
          </strong>
          <span className="operations-model-loading__description">Loading training metrics and feature weights…</span>
        </div>
      </section>
    );
  }

  const r2Pct = metrics ? Math.round(metrics.r2_score * 100) : 0;
  const modelReady = Boolean(metrics?.is_trained);
  const declaredMetrics = metrics as ModelMetricsWithProvenance | null;
  const trainingDataSource = declaredMetrics?.training_data_source;
  const validationScope = declaredMetrics?.validation_scope;
  const trainingSourceCounts = declaredMetrics?.training_source_counts;
  const observedTrainingRows = declaredMetrics?.observed_training_rows ?? 0;
  const provenanceDeclared = Boolean(trainingDataSource && validationScope);
  const isSyntheticValidation = source === 'offline'
    || trainingDataSource === 'synthetic_profile_generator';
  const isHistoryValidation = trainingDataSource?.startsWith('history_store_') ?? false;
  const isObservedHistory = trainingDataSource === 'history_store_observed';
  const isMixedHistory = trainingDataSource === 'history_store_mixed';

  const validationLabel = isSyntheticValidation
    ? 'Synthetic holdout'
    : isObservedHistory
      ? 'Observed-history holdout'
      : isMixedHistory
        ? 'Mixed-history holdout'
        : trainingDataSource === 'history_store_non_observed'
          ? 'Non-observed history holdout'
          : 'Provenance-undeclared holdout';

  const readinessLabel = !modelReady
    ? 'RF VALIDATION NOT LOADED'
    : source === 'offline'
      ? 'BUNDLED RF MANIFEST'
      : isSyntheticValidation
        ? 'BACKEND RF · SYNTHETIC'
        : isObservedHistory
          ? 'BACKEND RF · OBSERVED HISTORY'
          : isMixedHistory
            ? 'BACKEND RF · MIXED HISTORY'
            : trainingDataSource === 'history_store_non_observed'
              ? 'BACKEND RF · NON-OBSERVED HISTORY'
              : 'BACKEND RF · PROVENANCE UNDECLARED';

  const trainingSourceSummary = Object.entries(trainingSourceCounts ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, count]) => `${name.replace(/_/g, ' ')}: ${count.toLocaleString()}`)
    .join(' · ');

  return (
    <section
      className="glass-panel operations-model-panel"
      aria-labelledby="model-panel-title"
      aria-busy={loading}
    >
      <div className="operations-model-panel__header">
        <div className="operations-model-panel__intro">
          <div className="operations-model-panel__title-row">
            <Brain aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-indigo)" />
            <h3 id="model-panel-title" className="operations-model-panel__title">
              Random Forest Congestion Forecasting Engine
            </h3>
          </div>
        </div>

        <div className="operations-model-panel__badges">
          <span
            className={`badge operations-model-panel__status ${modelReady && (source === 'offline' || provenanceDeclared) ? 'badge-smooth' : 'badge-neutral'}`}
          >
            {readinessLabel}
          </span>
          <span className="badge badge-neutral">
            {source === 'offline' ? 'BUNDLED RF VALIDATION SNAPSHOT' : 'API MODEL STATUS'}
          </span>
        </div>
      </div>

      {error ? (
        <div
          role="alert"
          className="operations-inline-alert"
        >
          <TriangleAlert aria-hidden="true" size={ICON_SIZE.big} /> {error}
        </div>
      ) : null}

      <div className="operations-model-metrics">
        <article className="operations-model-metric">
          <div className="operations-model-metric__label">
            <Zap aria-hidden="true" size={ICON_SIZE.big} color="var(--accent-emerald)" /> {validationLabel} R²
          </div>
          <div className="operations-model-metric__value operations-model-metric__value--emerald">
            {modelReady ? metrics?.r2_score.toFixed(4) : 'Unavailable'}
          </div>
          <div className="operations-model-metric__detail">
            {modelReady ? `${r2Pct}% variance explained in ${validationLabel.toLowerCase()}` : 'No backend metric loaded'}
          </div>
        </article>

        <article className="operations-model-metric">
          <div className="operations-model-metric__label">
            <Activity aria-hidden="true" size={ICON_SIZE.big} color="var(--accent-cyan)" /> Mean Absolute Error (MAE)
          </div>
          <div className="operations-model-metric__value operations-model-metric__value--cyan">
            {modelReady ? `±${metrics?.mae.toFixed(2)}` : 'Unavailable'}{' '}
            <span className="operations-model-metric__unit">score pts</span>
          </div>
          <div className="operations-model-metric__detail">
            RMSE: {modelReady ? metrics?.rmse.toFixed(2) : 'Unavailable'}
          </div>
        </article>

        <article className="operations-model-metric">
          <div className="operations-model-metric__label">
            <Database aria-hidden="true" size={ICON_SIZE.big} color="var(--accent-indigo)" /> Model Training Size
          </div>
          <div className="operations-model-metric__value operations-model-metric__value--indigo">
            {modelReady ? metrics?.total_samples.toLocaleString() : 'Unavailable'}{' '}
            <span className="operations-model-metric__unit">rows</span>
          </div>
          <div className="operations-model-metric__detail">
            {!modelReady
              ? 'No backend training-size metric loaded'
              : isSyntheticValidation
                ? 'Deterministic synthetic training rows'
                : isHistoryValidation
                  ? `${observedTrainingRows.toLocaleString()} rows tagged observed`
                  : 'Backend row provenance was not declared'}
          </div>
        </article>
      </div>

      {featureEntries.length > 0 ? (
        <div aria-labelledby="feature-importance-title">
          <h4 id="feature-importance-title" className="operations-feature-importance__title">
            <Sparkles aria-hidden="true" size={ICON_SIZE.medium} color="var(--accent-cyan)" /> Model Feature Importance Weights
          </h4>
          <div className="operations-feature-importance__list">
            {featureEntries.map(([feature, value]) => {
              const percent = Math.round(value * 100);
              return (
                <div key={feature} className="operations-feature-importance__item">
                  <div className="operations-feature-importance__heading">
                    <span className="operations-feature-importance__name">{feature}</span>
                    <strong className="operations-feature-importance__value">{percent}%</strong>
                  </div>
                  <div
                    role="progressbar"
                    aria-label={`${feature} importance`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={percent}
                    className="ui-progress-track"
                  >
                    <div className="ui-progress-fill" style={{ width: `${percent}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="operations-feature-importance__empty">
          Feature-importance metrics have not been loaded for this model manifest.
        </div>
      )}

      <div
        role="note"
        className="operations-model-panel__provenance"
      >
        {isSyntheticValidation ? (
          <>These metrics measure fit to the deterministic synthetic profile generator; they are not real-world Batam accuracy claims.</>
        ) : isHistoryValidation ? (
          <>These metrics measure a chronological holdout from the stored history dataset. {observedTrainingRows.toLocaleString()} of {metrics?.total_samples.toLocaleString()} rows are tagged observed; the score is not an official Batam accuracy certification.</>
        ) : (
          <>The backend did not declare training-data provenance, so these metrics must be treated as unverified rather than assumed synthetic or observed.</>
        )}
        {trainingSourceSummary ? <> Training source counts: {trainingSourceSummary}.</> : null}
        {metrics?.last_trained_at ? (
          <> Trained <time dateTime={metrics.last_trained_at}>{formatModelTimestamp(metrics.last_trained_at)} SGT</time>.</>
        ) : null}
        {' '}
        <a
          href="https://dishub.batam.go.id/wp-content/uploads/sites/3/2025/02/DISHUB_LAKIP_2024.pdf"
          target="_blank"
          rel="noreferrer"
          className="operations-model-panel__source-link"
        >
          Official ATCS inventory <ExternalLink aria-hidden="true" size={ICON_SIZE.small} />
        </a>
        {!provenanceDeclared && source !== 'offline' ? ' · provenance manifest required' : null}
      </div>
    </section>
  );
};
