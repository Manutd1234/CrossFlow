import type { ModelMetrics } from '../types';

export type ModelTrainingDataSource =
  | 'synthetic_profile_generator'
  | 'history_store_observed'
  | 'history_store_mixed'
  | 'history_store_non_observed';

export type ModelValidationScope =
  | 'synthetic_holdout'
  | 'history_holdout_observed'
  | 'history_holdout_mixed'
  | 'history_holdout_non_observed';

/** Additive metadata understood by new clients without changing ModelMetrics. */
export type ModelMetricsWithProvenance = ModelMetrics & {
  training_data_source?: ModelTrainingDataSource;
  validation_scope?: ModelValidationScope;
  training_source_counts?: Record<string, number>;
  observed_training_rows?: number;
};

/**
 * Reproducible output of backend/models/congestion_model.py (seed 42).
 * This keeps model validation visible in a frontend-only deployment without
 * pretending that the browser has received fresh backend telemetry.
 */
export const BUNDLED_RF_VALIDATION: ModelMetricsWithProvenance = {
  is_trained: true,
  retraining_enabled: false,
  total_samples: 4000,
  r2_score: 0.9486,
  mae: 2.88,
  rmse: 3.62,
  last_trained_at: null,
  training_data_source: 'synthetic_profile_generator',
  validation_scope: 'synthetic_holdout',
  training_source_counts: { synthetic_profile_generator: 4000 },
  observed_training_rows: 0,
  feature_importances: {
    'Time of Day (Cyclical)': 0.559,
    'Weather Condition': 0.231,
    'Ferry Surge Proximity': 0.154,
    'Corridor Location': 0.032,
    'Weekend vs Weekday': 0.024,
  },
};
