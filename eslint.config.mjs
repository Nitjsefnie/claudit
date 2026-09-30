// Flat config (ESLint 9+), replacing the deleted .eslintrc.json.
//
// Generated from .eslintrc.json and reviewed by hand: every rule, every
// global and every parserOption below is the one that file carried, so
// the gate reports exactly what it reported before (issue #361). See
// eslint.yml for the derivation of the globals list.
//
// The three react/* rules stay: eslint-plugin-react 7.37.5 declares a peer
// range of eslint "^3 || ... || ^9.7", and the only supported line today
// is 10.x, so package.json pins the peer with an `overrides` entry rather
// than installing with --legacy-peer-deps (see that file's header).
import globals from 'globals';
import reactPlugin from 'eslint-plugin-react';

// Cross-file globals: names from public/index.html (React/ReactDOM CDN
// scripts, BACKEND_URL), every name the src files attach to window, and
// every top-level `function` declaration in src/**/*.js(x) — the files
// load as classic scripts, so a top-level function is a global.
// Derivation is mechanical (grep '^function NAME' over src); extend it the
// same way when adding files.
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
      // the ES built-ins from ecmaVersion, so the browser set is the part
      // that has to be named.
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
