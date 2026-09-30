// Flat config, replacing the deleted .eslintrc.json. ESLint 10 reads no
// eslintrc file at all — the @eslint/eslintrc dependency 8.57.1 carried is
// absent from 10.11.0's package.json — so the migration is forced, not a
// preference.
//
// Generated from .eslintrc.json and reviewed by hand: every RULE below is
// the one that file carried, and so is every cross-file global and every
// parserOption (issue #361). The browser ENVIRONMENT is the one deliberate
// exception, and it is not the same set:
//
//   .eslintrc.json's `env: {browser: true}` expanded through eslint 8.57.1,
//   which bundles globals@13.24.0 — 763 names. `globals.browser` here is
//   globals@17.12.0 — 1204. Measured by name in both directions: 464 added
//   (Navigation, Viewport, Temporal, Highlight, the WebGPU/WebUSB/WebXR
//   surfaces, `onpaste`, `oncut`, …) and 23 removed (applicationCache,
//   openDatabase, defaultStatus, AudioWorkletGlobalScope, SVGDiscardElement,
//   …). `Intl`, the one removed name that is not a browser API, is supplied
//   by ecmaVersion on both sides. OffscreenCanvas flips read-only.
//
// The added names are APIs browsers have shipped since 2022 and the removed
// ones are APIs browsers have dropped, so for a browser app the new set is
// the closer fit — but it is a different set, and a lint pass over the union
// of the two is what shows it. See eslint.yml for the derivation of the
// cross-file globals list, and PR 424 for the measurement.
//
// The three react/* rules stay: eslint-plugin-react 7.37.5 declares a peer
// range of eslint "^3 || ... || ^9.7", and the only supported line today
// is 10.x, so package.json pins the peer with an `overrides` entry rather
// than installing with --legacy-peer-deps (see that file's header).
import globals from 'globals';
import reactPlugin from 'eslint-plugin-react';

// Cross-file globals: names from public/index.html (React/ReactDOM CDN
// scripts, BACKEND_URL), every name the src files attach to window, and
// top-level names one src file uses from another — index.html loads each
// /src/* as a classic script, so those are globals.
//
// This is NOT every top-level declaration in src/: the tree defines 127
// top-level functions and this names 98, because a name only needs
// declaring once some other file references it bare. To extend it, add the
// name a new file needs — the gate tells you which: no-undef reports a
// cross-file reference this list is missing. tests/test_js_toolchain.py
// checks the two ends (nothing declared that the tree stopped defining,
// and the count).
const CROSS_FILE_GLOBALS = {
  ActivityHeatmapPanel: 'readonly',
  AgentDetail: 'readonly',
  App: 'readonly',
  BACKEND_URL: 'readonly',
  BurnRatePanel: 'readonly',
  CacheTTLPanel: 'readonly',
  CacheView: 'readonly',
  CodeBlock: 'readonly',
  ComparisonRow: 'readonly',
  ContextChart: 'readonly',
  ContextGrowthAgg: 'readonly',
  ContextGrowthPanel: 'readonly',
  ContextGrowthSessionDetail: 'readonly',
  ContextGrowthView: 'readonly',
  ContextSubPanel: 'readonly',
  CostBuckets: 'readonly',
  DashTooltip: 'readonly',
  Dashboard: 'readonly',
  EstimatedRateMark: 'readonly',
  EventDetail: 'readonly',
  ExportButton: 'readonly',
  HBar: 'readonly',
  KV: 'readonly',
  PerModelTable: 'readonly',
  Plain: 'readonly',
  ProjectPicker: 'readonly',
  RangePicker: 'readonly',
  React: 'readonly',
  ReactDOM: 'readonly',
  RefRow: 'readonly',
  RefsBlock: 'readonly',
  ReplyLatencyPanel: 'readonly',
  ResponseSizesPanel: 'readonly',
  SessionHeader: 'readonly',
  SessionView: 'readonly',
  SessionsList: 'readonly',
  Stat: 'readonly',
  StatsRow: 'readonly',
  SummaryStat: 'readonly',
  TOOL_COLORS: 'readonly',
  TYPE_META: 'readonly',
  Thinking: 'readonly',
  TimeSeriesPanel: 'readonly',
  TimelineRow: 'readonly',
  Tip: 'readonly',
  TokenBreakdownPanel: 'readonly',
  ToolCallDetail: 'readonly',
  ToolErrorRatePanel: 'readonly',
  ToolErrorSubPanel: 'readonly',
  ToolResultDetail: 'readonly',
  ToolUsagePanel: 'readonly',
  Tooltip: 'readonly',
  TopBar: 'readonly',
  TopTurnsTable: 'readonly',
  VBar: 'readonly',
  _matchRateKey: 'readonly',
  _normaliseModel: 'readonly',
  _toMillis: 'readonly',
  _toolColor: 'readonly',
  axisLabelDepthPx: 'readonly',
  axisLabelMaxChars: 'readonly',
  backendDashToShape: 'readonly',
  binMsLabel: 'readonly',
  buildSessionTurns: 'readonly',
  capForModel: 'readonly',
  computeSessionStats: 'readonly',
  computeSessions: 'readonly',
  computeTokenBreakdown: 'readonly',
  computeTurnStats: 'readonly',
  dashboardCol: 'readonly',
  dashboardTheme: 'readonly',
  datedRates: 'readonly',
  eventOneLine: 'readonly',
  extendBucketSeries: 'readonly',
  fmtDate: 'readonly',
  formatCell: 'readonly',
  generateSyntheticData: 'readonly',
  humanCurrency: 'readonly',
  humanFmt: 'readonly',
  inputPreview: 'readonly',
  modelColors: 'readonly',
  modelRates: 'readonly',
  monoAdvancePx: 'readonly',
  parseTranscript: 'readonly',
  perTurnStats: 'readonly',
  rateEpochs: 'readonly',
  rateForModel: 'readonly',
  renderEditDiff: 'readonly',
  resolveModelRate: 'readonly',
  shortModel: 'readonly',
  shortModelName: 'readonly',
  shortTime: 'readonly',
  timeTicksUTC: 'readonly',
  toolGlyph: 'readonly',
  txToDashData: 'readonly',
  usageCtxInput: 'readonly',
  vbarPadB: 'readonly',
  wrapAxisLabel: 'readonly'
};

export default [
  {
    // `.jsx` is not a default lint target in flat config (only .js/.cjs/.mjs
    // are), and the panels are .jsx, so both extensions are named here.
    files: ['**/*.js', '**/*.jsx'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'script',
      parserOptions: { ecmaFeatures: { jsx: true } },
      // eslintrc `env: {browser: true, es2024: true}`. Flat config derives
      // the ES built-ins from ecmaVersion — and its ES-2024 set is a superset
      // of the 28 names eslintrc's `env: {es2024: true}` supplied, so that
      // half is unchanged. The browser half is the measured difference the
      // file header describes.
      globals: {
        ...globals.browser,
        ...CROSS_FILE_GLOBALS,
      },
    },
    plugins: { react: reactPlugin },
    settings: { react: { version: '18.3.1' } },
    rules: {
      'no-undef': 'error',
      // `caughtErrors: 'none'` is eslintrc's default, spelled out because
      // ESLint 9 changed the default to 'all' — without this the gate would
      // report every `catch (e)` the tree writes on purpose (src/app.jsx has
      // four), which the eslintrc config never did.
      'no-unused-vars': ['error', { 'caughtErrors': 'none' }],
      'react/jsx-no-undef': ['error', { 'allowGlobals': true }],
      'react/jsx-uses-vars': 'error',
      'react/jsx-uses-react': 'error',
    },
  },
];
