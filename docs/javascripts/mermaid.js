// Keep the documentation repository small: Mermaid is loaded from a pinned CDN release
// rather than committing its generated, code-split JavaScript bundle.
import("https://cdn.jsdelivr.net/npm/mermaid@11.15.0/dist/mermaid.esm.min.mjs").then(({ default: mermaid }) => {
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });

  const render = () => {
    const diagrams = [...document.querySelectorAll(".mermaid:not([data-processed])")].filter(
      (diagram) => diagram.textContent.trim(),
    );
    if (diagrams.length) {
      mermaid.run({ nodes: diagrams }).catch((error) => {
        console.error("Unable to render Mermaid diagram:", error?.message || String(error));
      });
    }
  };

  document$.subscribe(render);
  new MutationObserver(render).observe(document.body, { childList: true, subtree: true });
  render();
});
