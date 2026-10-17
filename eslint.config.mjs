// ESLint 10 requires flat config. Browser globals come from globals.browser;
// cross-file names below reflect classic scripts loaded by public/index.html.
// package.json overrides eslint-plugin-react's peer range so installation
// uses the pinned toolchain without --legacy-peer-deps.
import globals from 'globals';
import reactPlugin from 'eslint-plugin-react';

// Cross-file globals: names from public/index.html (React/ReactDOM CDN
// scripts, BACKEND_URL), every name the src files attach to window, and
// top-level names one src file uses from another — index.html loads each
// /src/* as a classic script, so those are globals.
//
// Declare a top-level name here only when another file references it bare.
// no-undef reports missing cross-file names; tests/test_js_toolchain.py
// checks that declared names still exist.
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
      globals: {
        ...globals.browser,
        ...CROSS_FILE_GLOBALS,
      },
    },
    plugins: { react: reactPlugin },
    settings: { react: { version: '18.3.1' } },
    rules: {
      'no-undef': 'error',
      // Unused catch bindings are intentional; do not flag them.
      'no-unused-vars': ['error', { 'caughtErrors': 'none' }],
      'react/jsx-no-undef': ['error', { 'allowGlobals': true }],
      'react/jsx-uses-vars': 'error',
      'react/jsx-uses-react': 'error',
    },
  },
  {
    files: ['scripts/ci/js_coverage.mjs'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: globals.node,
    },
    rules: {
      'no-undef': 'error',
      'no-unused-vars': ['error', { 'caughtErrors': 'none' }],
    },
  },
];
