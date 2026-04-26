(() => {
  "use strict";

  const form = document.querySelector("#crawlForm");
  const sourceUrl = document.querySelector("#sourceUrl");
  const maxMatches = document.querySelector("#maxMatches");
  const exportButton = document.querySelector("#exportButton");
  const buttonSpinner = document.querySelector("#buttonSpinner");
  const buttonIcon = document.querySelector("#buttonIcon");
  const clearButton = document.querySelector("#clearButton");
  const copyButton = document.querySelector("#copyButton");
  const downloadButton = document.querySelector("#downloadButton");
  const statusBox = document.querySelector("#status");
  const preview = document.querySelector("#preview");
  const fileName = document.querySelector("#fileName");
  const fileSize = document.querySelector("#fileSize");
  const createdAt = document.querySelector("#createdAt");
  const storageLink = document.querySelector("#storageLink");
  const storageEmpty = document.querySelector("#storageEmpty");

  let currentOutput = "";
  let currentFileName = "";
  let currentFormat = "json";
  let currentSize = 0;
  let isBusy = false;

  function getApiPath() {
    return window.location.protocol === "file:"
      ? "http://127.0.0.1:5000/api/crawl"
      : "/api/crawl";
  }

  function getMergePath() {
    return window.location.protocol === "file:"
      ? "http://127.0.0.1:5000/api/merge"
      : "/api/merge";
  }

  function getStorageUploadPath() {
    return window.location.protocol === "file:"
      ? "http://127.0.0.1:5000/api/supabase/upload"
      : "/api/supabase/upload";
  }

  function selectedFormat() {
    return new FormData(form).get("format") || "json";
  }

  function normalizeUrl(value) {
    const trimmed = value.trim();
    if (!trimmed) {
      return "";
    }
    return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  }

  function parseSourceUrls(value) {
    const urls = [];
    const seen = new Set();
    for (const item of value.split(/[\s,]+/)) {
      const url = normalizeUrl(item);
      if (!url) {
        continue;
      }
      const key = url.toLowerCase();
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      urls.push(url);
    }
    return urls;
  }

  function slugify(value) {
    return value
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "crawl";
  }

  function buildFileName(urlValues, format) {
    const values = Array.isArray(urlValues) ? urlValues : [urlValues];
    if (values.length > 1) {
      return `bongda.${format === "txt" ? "txt" : "json"}`;
    }

    const urlValue = values[0] || "";
    let label = "crawl";
    try {
      const parsed = new URL(urlValue);
      const hostParts = parsed.hostname.replace(/^www\./, "").split(".").filter(Boolean);
      label = hostParts.length > 1 ? hostParts.slice(0, -1).join("-") : hostParts[0] || label;
    } catch (error) {
      label = urlValue;
    }
    return `${slugify(label)}.${format === "txt" ? "txt" : "json"}`;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) {
      return `${bytes} B`;
    }

    const units = ["KB", "MB", "GB"];
    let value = bytes / 1024;
    let index = 0;

    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }

    return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
  }

  function updateOutputButtons() {
    const hasOutput = Boolean(currentOutput && currentFileName);
    copyButton.disabled = !currentOutput;
    downloadButton.disabled = !hasOutput;
  }

  function setBusy(nextBusy, showSpinner = false) {
    isBusy = nextBusy;
    exportButton.disabled = nextBusy;
    clearButton.disabled = nextBusy;
    sourceUrl.disabled = nextBusy;
    maxMatches.disabled = nextBusy;
    buttonSpinner.classList.toggle("hidden", !showSpinner);
    buttonIcon.classList.toggle("hidden", showSpinner);
    updateOutputButtons();
  }

  function setStatus(message, isError = false) {
    statusBox.textContent = message;
    statusBox.classList.toggle("is-error", isError);
  }

  function setPreview(text) {
    currentOutput = text;
    preview.textContent = text || "Chưa có dữ liệu";
    preview.classList.toggle("empty", !text);
    updateOutputButtons();
  }

  function resetStorageLink() {
    storageLink.href = "#";
    storageLink.classList.add("hidden");
    storageEmpty.classList.remove("hidden");
  }

  function setStorageLink(url) {
    storageLink.href = url;
    storageLink.textContent = "Mở link";
    storageLink.classList.remove("hidden");
    storageEmpty.classList.add("hidden");
  }

  function downloadFile(name, content, format) {
    const type = format === "json" ? "application/json;charset=utf-8" : "text/plain;charset=utf-8";
    const blob = new Blob([content], { type });
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");

    anchor.href = objectUrl;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);

    return blob.size;
  }

  async function readError(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const data = await response.json();
      return data.error || JSON.stringify(data);
    }
    return response.text();
  }

  function fallbackCopyText(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.focus({ preventScroll: true });
    textarea.select();

    try {
      return document.execCommand("copy");
    } catch (error) {
      return false;
    } finally {
      textarea.remove();
    }
  }

  async function copyText(text) {
    if (navigator.clipboard?.writeText && document.hasFocus()) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (error) {
        return fallbackCopyText(text);
      }
    }

    return fallbackCopyText(text);
  }

  async function crawlOutput(urlValues, format, maxValue) {
    const apiFormat = format === "txt" ? "m3u" : "json";
    const values = Array.isArray(urlValues) ? urlValues : [urlValues];
    let response;

    if (values.length > 1) {
      response = await fetch(getMergePath(), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: format === "json" ? "application/json" : "text/plain",
        },
        body: JSON.stringify({
          links: values,
          format: apiFormat,
          max: maxValue,
        }),
      });
    } else {
      const params = new URLSearchParams({
        format: apiFormat,
        link: values[0],
        max: String(maxValue),
      });

      response = await fetch(`${getApiPath()}?${params.toString()}`, {
        headers: {
          Accept: format === "json" ? "application/json" : "text/plain",
        },
      });
    }

    if (!response.ok) {
      throw new Error(await readError(response));
    }

    return format === "json"
      ? JSON.stringify(await response.json(), null, 2)
      : await response.text();
  }

  async function uploadOutput(filename, format, content) {
    const response = await fetch(getStorageUploadPath(), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        filename,
        format,
        content,
      }),
    });

    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || "Không lấy được link");
    }
    return data;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const urlValues = parseSourceUrls(sourceUrl.value);
    const format = selectedFormat();
    const maxValue = Math.max(1, Math.min(Number(maxMatches.value) || 80, 80));

    if (!urlValues.length) {
      setStatus("Nhập link nguồn", true);
      sourceUrl.focus();
      return;
    }

    currentFileName = "";
    currentFormat = format;
    currentSize = 0;
    resetStorageLink();
    setPreview("");
    setBusy(true, true);

    try {
      const name = buildFileName(urlValues, format);
      const isMerge = urlValues.length > 1;

      setStatus("Đang crawl...");
      if (isMerge) {
        setStatus("\u0110ang g\u1ed9p danh s\u00e1ch...");
      }
      const output = await crawlOutput(urlValues, format, maxValue);
      const size = new Blob([output], {
        type: format === "json" ? "application/json;charset=utf-8" : "text/plain;charset=utf-8",
      }).size;

      currentFileName = name;
      currentFormat = format;
      currentSize = size;
      setPreview(output);
      fileName.textContent = name;
      fileSize.textContent = formatBytes(size);
      createdAt.textContent = new Date().toLocaleString("vi-VN");

      setStatus("Đang upload và lấy link...");
      const uploaded = await uploadOutput(name, format, output);
      const link = uploaded.direct_url || uploaded.shared_url;
      setStorageLink(link);
      const copied = await copyText(link);
      setStatus(copied ? "Đã lấy link và sao chép" : "Đã lấy link. Mở link để sao chép thủ công");
    } catch (error) {
      setStatus(error.message || "Không lấy được link", true);
    } finally {
      setBusy(false);
    }
  });

  clearButton.addEventListener("click", () => {
    setPreview("");
    currentFileName = "";
    currentFormat = "json";
    currentSize = 0;
    fileName.textContent = "-";
    fileSize.textContent = "-";
    createdAt.textContent = "-";
    resetStorageLink();
    setStatus("Sẵn sàng");
    sourceUrl.focus();
  });

  copyButton.addEventListener("click", async () => {
    if (!currentOutput) {
      return;
    }

    const copied = await copyText(currentOutput);
    setStatus(copied ? "Đã sao chép nội dung" : "Trình duyệt đang chặn sao chép tự động", !copied);
  });

  downloadButton.addEventListener("click", () => {
    if (!currentOutput || !currentFileName) {
      return;
    }

    const size = downloadFile(currentFileName, currentOutput, currentFormat);
    currentSize = size;
    fileSize.textContent = formatBytes(size);
    setStatus("Đã tải file");
  });
})();
