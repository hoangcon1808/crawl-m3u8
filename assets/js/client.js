(() => {
    "use strict";
    
    const form = document.querySelector("#crawlForm");
    const sourceUrl = document.querySelector("#sourceUrl");
    const maxMatches = document.querySelector("#maxMatches");
    const exportButton = document.querySelector("#exportButton");
    const txtPrintButton = document.querySelector("#txtPrintButton");
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

    // --- HELPER FUNCTIONS ---
    
    function getApiPath() {
        return window.location.protocol === "file:" ? "http://127.0.0.1:5000/api/crawl" : "/api/crawl";
    }

    function getMergePath() {
        return window.location.protocol === "file:" ? "http://127.0.0.1:5000/api/merge" : "/api/merge";
    }

    // ĐÃ SỬA: Trỏ về API lưu Database thay vì Supabase
    function getDatabaseSavePath() {
        return window.location.protocol === "file:" ? "http://127.0.0.1:5000/api/database/save" : "/api/database/save";
    }

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
            if (!url) continue;
            const key = url.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            urls.push(url);
        }
        return urls;
    }

    function slugify(value) {
        return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
            .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "crawl";
    }

    function buildFileName(urlValues, format) {
        const values = Array.isArray(urlValues) ? urlValues : [urlValues];
        const ext = format === "json" ? "json" : (format === "txt" ? "txt" : "m3u");
        if (values.length > 1) return `bongda.${ext}`;
        
        const urlValue = values[0] || "";
        let label = "crawl";
        try {
            const parsed = new URL(urlValue);
            const hostParts = parsed.hostname.replace(/^www\./, "").split(".").filter(Boolean);
            label = hostParts.length > 1 ? hostParts.slice(0, -1).join("-") : hostParts[0] || label;
        } catch (error) {
            label = urlValue;
        }
        return `${slugify(label)}.${ext}`;
    }

    function formatBytes(bytes) {
        if (bytes < 1024) return `${bytes} B`;
        const units = ["KB", "MB", "GB"];
        let value = bytes / 1024;
        let index = 0;
        while (value >= 1024 && index < units.length - 1) {
            value /= 1024;
            index += 1;
        }
        return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
    }

    // Hàm chuyển đổi JSON sang M3U (TXT)
    function convertJsonToM3u(jsonString) {
        try {
            const data = JSON.parse(jsonString);
            if (!Array.isArray(data)) return jsonString;
            let m3u = "#EXTM3U\n";
            data.forEach(item => {
                const title = item.title || "Kênh không tên";
                const url = item.url || item.stream_url || "";
                if (url) m3u += `#EXTINF:-1,${title}\n${url}\n`;
            });
            return m3u;
        } catch (e) {
            console.error("Lỗi parse JSON:", e);
            return jsonString;
        }
    }

    function updateOutputButtons() {
        const hasOutput = Boolean(currentOutput && currentFileName);
        copyButton.disabled = !currentOutput;
        downloadButton.disabled = !hasOutput;
        if (txtPrintButton) txtPrintButton.disabled = !currentOutput || currentFormat !== "json";
    }

    function setBusy(nextBusy, showSpinner = false) {
        isBusy = nextBusy;
        exportButton.disabled = nextBusy;
        if (txtPrintButton) txtPrintButton.disabled = nextBusy;
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

    async function copyText(text) {
        if (navigator.clipboard?.writeText && document.hasFocus()) {
            try {
                await navigator.clipboard.writeText(text);
                return true;
            } catch (error) {
                return false;
            }
        }
        return false;
    }

    // --- API CORE ---
    
    async function crawlOutput(urlValues, format, maxValue) {
        const apiFormat = format === "txt" ? "m3u" : "json";
        const values = Array.isArray(urlValues) ? urlValues : [urlValues];
        let response;
        if (values.length > 1) {
            response = await fetch(getMergePath(), {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Accept: format === "json" ? "application/json" : "text/plain"
                },
                body: JSON.stringify({ links: values, format: apiFormat, max: maxValue }),
            });
        } else {
            const params = new URLSearchParams({ format: apiFormat, link: values[0], max: String(maxValue) });
            response = await fetch(`${getApiPath()}?${params.toString()}`, {
                headers: {
                    Accept: format === "json" ? "application/json" : "text/plain"
                },
            });
        }
        if (!response.ok) throw new Error(await readError(response));
        return format === "json" ? JSON.stringify(await response.json(), null, 2) : await response.text();
    }

    // ĐÃ SỬA: Sử dụng getDatabaseSavePath()
    async function uploadOutput(filename, format, content) {
        const response = await fetch(getDatabaseSavePath(), {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json"
            },
            body: JSON.stringify({ filename, format, content }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "Không tạo được link trên Database");
        return data;
    }

    // --- EVENTS ---
    
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
            setStatus(urlValues.length > 1 ? "Đang gộp..." : "Đang crawl...");
            const output = await crawlOutput(urlValues, format, maxValue);
            const size = new Blob([output]).size;
            
            currentFileName = name;
            currentFormat = format;
            currentSize = size;
            
            setPreview(output);
            fileName.textContent = name;
            fileSize.textContent = formatBytes(size);
            createdAt.textContent = new Date().toLocaleString("vi-VN");
            
            setStatus("Đang lưu vào Database...");
            const uploaded = await uploadOutput(name, format, output);
            
            // ĐÃ SỬA: Đọc chuẩn URL từ API Flask trả về
            const link = uploaded.url; 
            setStorageLink(link);
            
            const copied = await copyText(link);
            setStatus(copied ? "Đã lấy link và sao chép" : "Đã lấy link.");
            
        } catch (error) {
            setStatus(error.message, true);
        } finally {
            setBusy(false);
        }
    });

    // Event Xử lý nút Hiện TXT (M3U) từ JSON
    if (txtPrintButton) {
        txtPrintButton.addEventListener("click", () => {
            if (!currentOutput || currentFormat !== "json") return;
            setStatus("Đang chuyển đổi JSON sang M3U...");
            const m3uText = convertJsonToM3u(currentOutput);
            
            // Cập nhật trạng thái sang TXT
            currentOutput = m3uText;
            currentFormat = "txt";
            currentFileName = currentFileName.replace(".json", ".txt");
            const newSize = new Blob([m3uText]).size;
            
            // In ra màn hình
            setPreview(m3uText);
            fileName.textContent = currentFileName;
            fileSize.textContent = formatBytes(newSize);
            setStatus("Đã hiển thị dạng TXT (M3U)");
        });
    }

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
        if (!currentOutput) return;
        const copied = await copyText(currentOutput);
        setStatus(copied ? "Đã sao chép nội dung" : "Lỗi sao chép", !copied);
    });

    downloadButton.addEventListener("click", () => {
        if (!currentOutput || !currentFileName) return;
        const size = downloadFile(currentFileName, currentOutput, currentFormat);
        fileSize.textContent = formatBytes(size);
        setStatus("Đã tải file");
    });
})();
