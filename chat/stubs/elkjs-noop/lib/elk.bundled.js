// Agent OS (MIT): no-op stand-in for elkjs (EPL-2.0). `layout: elk` diagrams fail -> the chat shows the diagram source.
class ELK { layout() { return Promise.reject(new Error("ELK layout is disabled in Agent OS (licence policy)")); } knownLayoutAlgorithms() { return Promise.resolve([]); } knownLayoutOptions() { return Promise.resolve([]); } knownLayoutCategories() { return Promise.resolve([]); } }
module.exports = ELK; module.exports.default = ELK;
