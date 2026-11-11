// ESLint 10 requires flat config. src/ takes browser globals from
// globals.browser.
// package.json overrides the plugin's peer range (no --legacy-peer-deps),
// forces minimatch 11: its minimatch rules can throw.
import globals from 'globals';
import reactPlugin from 'eslint-plugin-react';

// Cross-file globals: names from public/index.html (React/ReactDOM from
// /vendor/, BACKEND_URL), every name src files attach to window, and
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

// Browser names each script reads in the functions it runs in the page.
const PAGE_GLOBALS = {
  feature_surfaces: ['document', 'window'],
  layout_waits: ['document', 'getComputedStyle', 'requestAnimationFrame', 'requestIdleCallback'],
  panel_interactions: ['React', 'ReactDOM', 'document', 'getComputedStyle', 'requestAnimationFrame', 'window'],
  panel_interactions_report: ['HTMLElement', 'document'],
  panel_layout: ['document', 'window'],
  panel_layout_rules: ['MutationObserver', 'document', 'getComputedStyle', 'requestAnimationFrame', 'window'],
  picker_probe: ['MutationObserver', 'cancelAnimationFrame', 'document', 'getComputedStyle', 'requestAnimationFrame'],
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
    files: ['scripts/ci/*.mjs', 'eslint.config.mjs'],
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
  ...Object.entries(PAGE_GLOBALS).map(([script, names]) => ({
    files: [`scripts/ci/${script}.mjs`],
    languageOptions: {
      globals: Object.fromEntries(names.map(name => [name, 'readonly'])),
    },
  })),
];
