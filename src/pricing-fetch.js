// pricing-loader.js's fetch, then one install step per task.
window.fetchPricing = (url, refuse, install) => fetch(
  url, { headers: { 'Cache-Control': 'no-cache' } },
).then((response) => {
  if (response.status !== 200) throw refuse(`HTTP ${response.status} from ${url}`);
  // Signed out, this redirects to the sign-in page, a 200.
  if (response.url !== url) throw refuse(`redirected to ${response.url}`);
  return response.text();
}).then((text) => new Promise((resolve, reject) => {
  const steps = install(text);
  const channel = new MessageChannel();
  channel.port1.onmessage = () => {
    try {
      if (!steps.next().done) {
        channel.port2.postMessage(null);
        return;
      }
      channel.port1.close();
      resolve();
    } catch (e) {
      channel.port1.close();
      reject(e);
    }
  };
  channel.port2.postMessage(null);
}));
