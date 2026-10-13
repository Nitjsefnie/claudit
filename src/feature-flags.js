"use strict";

window.featureOn = function featureOn(key) {
  const features = window.FEATURES;
  if (!features || !Object.prototype.hasOwnProperty.call(features, key)) {
    return true;
  }
  return features[key] !== false;
};
