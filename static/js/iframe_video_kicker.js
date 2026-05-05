// Robust same-origin iframe video starter.
//
// Chrome's autoplay policy + lazy iframes + many simultaneous decoders means a
// one-shot .play() on iframe load is not enough: the promise resolves before
// the decoder has a frame, so paused === false while currentTime stays 0 and
// the cell renders black. This kicker:
//   1. Waits for each iframe to scroll near the viewport (matches loading=lazy).
//   2. Walks every <video> inside (and watches for newly-mounted ones).
//   3. Calls play() on each readiness milestone, not just once.
//   4. Polls currentTime; if stuck at 0, force-reloads and replays.
(function () {
    if (window.IframeVideoKicker) return;

    function armVideo(video) {
        if (video.__kickerArmed) return;
        video.__kickerArmed = true;
        try {
            video.muted = true;
            video.defaultMuted = true;
            video.playsInline = true;
            video.setAttribute("playsinline", "");
            video.setAttribute("muted", "");
            video.setAttribute("webkit-playsinline", "");
            if (!video.preload || video.preload === "none") video.preload = "auto";
        } catch (_) {}

        var tryPlay = function () {
            try {
                var p = video.play();
                if (p && typeof p.catch === "function") p.catch(function () {});
            } catch (_) {}
        };

        ["loadedmetadata", "loadeddata", "canplay", "canplaythrough"]
            .forEach(function (ev) { video.addEventListener(ev, tryPlay); });

        var lastT = -1;
        var stuckTicks = 0;
        var watch = setInterval(function () {
            if (!video.isConnected) { clearInterval(watch); return; }
            if (video.readyState < 2) { tryPlay(); return; }
            if (video.paused) { tryPlay(); return; }
            if (video.currentTime > 0.05 && video.currentTime !== lastT) {
                lastT = video.currentTime;
                stuckTicks = 0;
                return;
            }
            if (video.currentTime === lastT) {
                stuckTicks++;
                if (stuckTicks >= 4) {
                    try { video.load(); } catch (_) {}
                    tryPlay();
                    stuckTicks = 0;
                }
            }
            lastT = video.currentTime;
        }, 500);
        // Stop watchdog after 60s of healthy operation; a paused-then-resumed
        // video will be re-armed by the iframe's MutationObserver if needed.
        setTimeout(function () { clearInterval(watch); }, 60000);
    }

    function armAllVideos(doc) {
        try { doc.querySelectorAll("video").forEach(armVideo); } catch (_) {}
    }

    function attachToIframe(iframe) {
        if (iframe.__kickerAttached) return;
        iframe.__kickerAttached = true;

        var setup = function () {
            try {
                var d = iframe.contentDocument;
                if (!d) return;
                armAllVideos(d);
                if (window.MutationObserver) {
                    var mo = new MutationObserver(function () { armAllVideos(d); });
                    mo.observe(d.documentElement, { childList: true, subtree: true });
                    iframe.__kickerObserver = mo;
                }
            } catch (_) { /* cross-origin or torn down */ }
        };

        iframe.addEventListener("load", setup);
        try {
            if (iframe.contentDocument
                && iframe.contentDocument.readyState !== "loading") {
                setup();
            }
        } catch (_) {}
    }

    function attachAll(selector) {
        var nodes = document.querySelectorAll(selector);
        if (!nodes.length) return;
        if ("IntersectionObserver" in window) {
            var io = new IntersectionObserver(function (entries) {
                entries.forEach(function (e) {
                    if (e.isIntersecting) attachToIframe(e.target);
                });
            }, { rootMargin: "200px" });
            nodes.forEach(function (i) { io.observe(i); });
        }
        // Always also attach immediately so already-visible iframes don't wait
        // for an intersection event that may have already fired.
        nodes.forEach(attachToIframe);
    }

    window.IframeVideoKicker = {
        attachAll: attachAll,
        attachToIframe: attachToIframe,
    };
})();
