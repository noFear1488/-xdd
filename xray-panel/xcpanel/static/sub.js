"use strict";

(function () {
  const data = JSON.parse(document.getElementById("sub-data").textContent);

  function svg(text) {
    for (const level of ["M", "L"]) {
      try {
        const qr = qrcode(0, level);
        qr.addData(text);
        qr.make();
        return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
      } catch (_) { /* too long at this level */ }
    }
    return "";
  }

  document.getElementById("sub-qr").innerHTML = svg(data.sub);

  const modal = document.getElementById("qr-modal");
  document.addEventListener("click", async (e) => {
    const copyButton = e.target.closest("[data-copy]");
    if (copyButton) {
      const text = copyButton.getAttribute("data-copy");
      try { await navigator.clipboard.writeText(text); } catch (_) {
        const area = document.createElement("textarea");
        area.value = text; document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
      }
      const label = copyButton.textContent;
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = label; }, 1500);
    }
    const qrButton = e.target.closest("[data-qr]");
    if (qrButton) {
      document.getElementById("qr-big").innerHTML = svg(qrButton.getAttribute("data-qr"));
      modal.hidden = false;
    }
    if (e.target.closest("[data-close]") || e.target === modal) modal.hidden = true;
  });
})();
