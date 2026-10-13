// Historical telemetry can name a feature that is disabled on this page.
// Keep collection/totals intact; remove its named presentation dimensions.
(function () {
  const panels = {
    usage_timeseries: ['input_tokens', 'output_tokens', 'cache_create', 'cache_read', 'total_tokens', 'cost_usd'],
    thinking_output: ['thinking_output'], line_churn: ['lines_added', 'lines_deleted'],
    model_comparison: ['cost_by_model', 'tokens_by_model'],
    token_breakdown: ['token_breakdown_by_tokens', 'token_breakdown_by_cost'],
    project_comparison: ['cost_by_project', 'tokens_by_project'],
    cache_ttl: ['prompt_cache_ttl_split'], response_sizes: ['response_sizes'],
    context_growth: ['context_growth_comparison'], burn_rate: ['session_burn_rate'],
    context_comparison: ['cost_by_context_size', 'tokens_by_context_size'],
    tool_usage: ['tool_usage_ratio'], reply_latency: ['reply_latency'],
    page_performance: ['page_performance'], tool_error_rate: ['tool_error_rate'],
    agent_comparison: ['cost_by_agent_type', 'tokens_by_agent_type'],
    activity_heatmap: ['activity_heatmap', 'activity_heatmap_legend'],
    inspector: ['context_chart'],
  };
  const regions = Object.fromEntries(Object.entries(panels).flatMap(([feature, names]) =>
    names.map(name => ['panel_' + name, feature])));
  Object.assign(regions, {inspector:'inspector', project_picker:'project_picker', summary_strip:'summary'});

  window.perfFeatureVisible = function perfFeatureVisible(dimension, value) {
    if (dimension === 'metric' && value === 'inspector_open') return window.featureOn('inspector');
    if (dimension === 'phase' && value === 'sse_update') return window.featureOn('live_updates');
    return dimension !== 'region' || !regions[value] || window.featureOn(regions[value]);
  };
})();
