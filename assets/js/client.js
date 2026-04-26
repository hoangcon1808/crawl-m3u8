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
    let isBusy = false;

    // Tự động xác định Base URL (Local hoặc Production)
    const BASE_URL = window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost" 
                     ? "http://127.0.0.1:5000" 
                     : "";

    const PATHS = {
        crawl: `${BASE_URL}/api/crawl`,
        merge: `${BASE_URL}/api/merge`,
        upload: `${BASE_URL}/api/supabase/upload`
    };

    function selectedFormat() {
        return new FormData(form).get("format") || "json";
    }

    function normalizeUrl(value) {
        const trimmed = value.trim();
        if (!trimmed) return "";
        return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
    }

    function parseSourceUrls(value) {
        const urls = [];
        const seen = new Set();
        for (const item of value.split(/[\s,]+/)) {
            const url = normalizeUrl(item);
            if (url && !seen.has(url.toLowerCase())) {
                seen.add(url.toLowerCase());
                urls.push(url);
            }
        }
        return urls;
    }

    function slugify(value) {
        return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
            .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "crawl";
    }

    function buildFileName(urlValues, format) {
        const ext = format === "json" ? "json" : "m3u";
        if (urlValues.length > 1) return `merged-playlist.${ext}`;
        try {
            const host = new URL(urlValues[0]).hostname.replace("www.", "").split(".")[0];
            return `${slugify(host)}.${ext}`;
        } catch {
            return `crawl-result.${ext}`;
        }
    }

    function formatBytes(bytes) {
        if (bytes === 0) return "0 B";
        const k = 1024, units = ["B", "KB", "MB"], i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + units[i];
    }

    function setBusy(busy) {
        isBusy = busy;
        exportButton.disabled = busy;
        sourceUrl.disabled = busy;
        buttonSpinner.classList.toggle("d-none", !busy);
        buttonIcon.classList.toggle("d-none", busy);
    }

    function setStatus(msg, isError = false) {
        statusBox.textContent = msg;
        statusBox.className = isError ? "text-danger small" : "text-muted small";
    }

    function updatePreview(text) {
        currentOutput = text;
        preview.textContent = text || "Chưa có dữ liệu";
        copyButton.disabled = !text;
        downloadButton.disabled = !text;
    }

    async function copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch {
            const input = document.createElement("textarea");
            input.value = text; document.body.appendChild(input);
            input.select(); const ok = document.execCommand("copy");
            document.body.removeChild(input);
            return ok;
        }
    }

    // --- API CALLS ---

    async function crawlData(urls, format, max) {
        const apiFmt = format === "json" ? "json" : "m3u";
        if (urls.length > 1) {
            const res = await fetch(PATHS.merge, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ links: urls, format: apiFmt, max })
            });
            if (!res.ok) throw new Error("Lỗi khi gộp nguồn");
            return apiFmt === "json" ? JSON.stringify(await res.json(), null, 2) : await res.text();
        } else {
            const res = await fetch(`${PATHS.crawl}?link=${encodeURIComponent(urls[0])}&format=${apiFmt}&max=${max}`);
            if (!res.ok) throw new Error("Lỗi khi cào dữ liệu");
            return apiFmt === "json" ? JSON.stringify(await res.json(), null, 2) : await res.text();
        }
    }

    async function uploadToSupabase(filename, content) {
        const res = await fetch(PATHS.upload, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename, content })
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || "Lỗi upload");
        return data; // Chứa { ok: true, url: "..." }
    }

    // --- MAIN EVENT ---

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (isBusy) return;

        const urls = parseSourceUrls(sourceUrl.value);
        const format = selectedFormat();
        const max = maxMatches.value || 100;

        if (!urls.length) return setStatus("Vui lòng nhập link!", true);

        setBusy(true);
        setStatus("Đang xử lý dữ liệu...");
        updatePreview("");

        try {
            // 1. Crawl dữ liệu
            const output = await crawlData(urls, format, max);
            currentOutput = output;
            currentFileName = buildFileName(urls, format);
            
            // 2. Cập nhật giao diện
            updatePreview(output);
            fileName.textContent = currentFileName;
            const size = new Blob([output]).size;
            fileSize.textContent = formatBytes(size);
            createdAt.textContent = new Date().toLocaleTimeString();

            // 3. Tự động upload lên Supabase
            setStatus("Đang lưu lên Cloud...");
            const uploadRes = await uploadToSupabase(currentFileName, output);
            
            // 4. Hiển thị link
            storageLink.href = uploadRes.url;
            storageLink.classList.remove("d-none");
            storageEmpty.classList.add("d-none");

            const copied = await copyText(uploadRes.url);
            setStatus(copied ? "Hoàn tất! Đã sao chép link." : "Hoàn tất! Đã có link.");

        } catch (err) {
            setStatus(err.message, true);
        } finally {
            setBusy(false);
        }
    });

    // Clear
    clearButton.addEventListener("click", () => {
        sourceUrl.value = "";
        updatePreview("");
        fileName.textContent = "-";
        fileSize.textContent = "-";
        createdAt.textContent = "-";
        storageLink.classList.add("d-none");
        storageEmpty.classList.remove("d-none");
        setStatus("Sẵn sàng");
    });

    // Copy Content
    copyButton.addEventListener("click", async () => {
        if (await copyText(currentOutput)) setStatus("Đã sao chép nội dung!");
    });

    // Download
    downloadButton.addEventListener("click", () => {
        const blob = new Blob([currentOutput], { type: "text/plain" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = currentFileName; a.click();
        URL.revokeObjectURL(url);
    });

})();
