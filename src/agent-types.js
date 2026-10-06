// Canonical agent-role names in the Inspector (issue #691). The fold
// TABLE is the backend's (backend/agent_types.py, issue #650): the served
// page injects it as window.AGENT_TYPE_FOLD, so no literal copy of the
// table lives in the browser. Without it the lookup degrades to the
// namespace split, and an unknown role passes through verbatim.
window.canonicalAgentType = function (name) {
  const k = name.includes(':') ? name.slice(name.lastIndexOf(':') + 1) : name;
  return (window.AGENT_TYPE_FOLD || {})[k] || k;
};
