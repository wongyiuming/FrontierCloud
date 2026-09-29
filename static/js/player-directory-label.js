(() => {
    const RETRY_BASE_MS = 350;
    const RETRY_MAX_MS = 3000;
    const RETRY_EXPONENT_CAP = 4;

    function installContinuousAudioFetchRetry() {
        if (typeof PLAYER_KIND === 'undefined' || PLAYER_KIND !== 'audio') return;
        if (window.frontierCloudContinuousFetchRetry?.installed) return;

        const nativeFetch = window.fetch.bind(window);
        let retryCount = 0;
        let resumeCount = 0;

        function mediaStreamRequest(input, init) {
            if (init?.credentials !== 'same-origin') return false;
            const raw = typeof input === 'string' || input instanceof URL
                ? String(input)
                : String(input?.url || '');
            if (!raw) return false;
            try {
                const url = new URL(raw, window.location.href);
                return url.origin === window.location.origin
                    && url.pathname === '/api/v1/media/stream';
            } catch (_error) {
                return false;
            }
        }

        function currentPlaybackGeneration() {
            try {
                return typeof playerSwitchSequence === 'number' ? playerSwitchSequence : null;
            } catch (_error) {
                return null;
            }
        }

        function generationIsCurrent(generation) {
            if (generation === null) return true;
            try {
                return typeof playerSwitchSequence === 'number' && playerSwitchSequence === generation;
            } catch (_error) {
                return false;
            }
        }

        function stalePlaybackError() {
            try {
                return new DOMException('Playback changed', 'AbortError');
            } catch (_error) {
                const error = new Error('Playback changed');
                error.name = 'AbortError';
                return error;
            }
        }

        function retryDelay(attempt) {
            return Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** Math.min(attempt, RETRY_EXPONENT_CAP)));
        }

        async function waitForRetry(attempt, generation, signal) {
            if (!generationIsCurrent(generation) || signal?.aborted) throw stalePlaybackError();
            const delay = retryDelay(attempt);
            await new Promise((resolve, reject) => {
                const timer = window.setTimeout(resolve, delay);
                if (!signal) return;
                const abort = () => {
                    window.clearTimeout(timer);
                    reject(signal.reason || stalePlaybackError());
                };
                signal.addEventListener('abort', abort, {once: true});
            });
            if (!generationIsCurrent(generation) || signal?.aborted) throw stalePlaybackError();
        }

        function contentRange(headers) {
            const raw = String(headers?.get?.('Content-Range') || '');
            const match = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i.exec(raw);
            if (!match) return null;
            return {
                start: Number(match[1]),
                end: Number(match[2]),
                total: match[3] === '*' ? 0 : Number(match[3]),
            };
        }

        async function requestUntilReadable(input, init, offset, generation) {
            let attempt = 0;
            while (generationIsCurrent(generation) && !init?.signal?.aborted) {
                let response = null;
                try {
                    const headers = new Headers(init?.headers || {});
                    if (offset > 0) headers.set('Range', `bytes=${offset}-`);
                    response = await nativeFetch(input, {...init, headers});
                    if (response.ok && response.body) {
                        if (offset === 0) return response;
                        const range = contentRange(response.headers);
                        if (response.status === 206 && range?.start === offset) return response;
                    }
                } catch (_error) {
                    // Network failures are transient for continuous-audio prefetch.
                }
                try {
                    await response?.body?.cancel?.();
                } catch (_error) {
                    // Best-effort cleanup only.
                }
                retryCount += 1;
                await waitForRetry(attempt, generation, init?.signal);
                attempt += 1;
            }
            throw stalePlaybackError();
        }

        async function resilientMediaFetch(input, init = {}) {
            if (!mediaStreamRequest(input, init)) return nativeFetch(input, init);

            const generation = currentPlaybackGeneration();
            const first = await requestUntilReadable(input, init, 0, generation);
            const exposedHeaders = new Headers(first.headers);
            const initialRange = contentRange(first.headers);
            let totalBytes = initialRange?.total || Number(first.headers.get('Content-Length') || 0);
            if (totalBytes > 0) exposedHeaders.set('Content-Length', String(totalBytes));

            let reader = first.body.getReader();
            let offset = 0;
            let closed = false;

            const body = new ReadableStream({
                async pull(controller) {
                    while (!closed) {
                        if (!generationIsCurrent(generation) || init?.signal?.aborted) {
                            closed = true;
                            controller.error(stalePlaybackError());
                            return;
                        }

                        try {
                            const {done, value} = await reader.read();
                            if (!done) {
                                offset += value.byteLength;
                                controller.enqueue(value);
                                return;
                            }
                            if (!totalBytes || offset >= totalBytes) {
                                closed = true;
                                controller.close();
                                return;
                            }
                        } catch (_error) {
                            if (!generationIsCurrent(generation) || init?.signal?.aborted) {
                                closed = true;
                                controller.error(stalePlaybackError());
                                return;
                            }
                        }

                        const resumed = await requestUntilReadable(input, init, offset, generation);
                        const range = contentRange(resumed.headers);
                        if (range?.total > 0) totalBytes = range.total;
                        reader = resumed.body.getReader();
                        resumeCount += 1;
                    }
                },
                async cancel(reason) {
                    closed = true;
                    try {
                        await reader.cancel(reason);
                    } catch (_error) {
                        // Best-effort cleanup only.
                    }
                },
            });

            return new Response(body, {
                status: first.status,
                statusText: first.statusText,
                headers: exposedHeaders,
            });
        }

        window.fetch = resilientMediaFetch;
        window.frontierCloudContinuousFetchRetry = {
            installed: true,
            base_delay_ms: RETRY_BASE_MS,
            max_delay_ms: RETRY_MAX_MS,
            status: () => ({retry_count: retryCount, resume_count: resumeCount}),
        };
    }

    installContinuousAudioFetchRetry();

    const host = document.getElementById('playerDirectoryLabel');
    if (!host) return;

    const rawPath = new URLSearchParams(window.location.search).get('path') || '';
    const parts = rawPath.replace(/\\/g, '/').split('/').filter(Boolean);
    const relativeParts = (parts[0] === 'music' || parts[0] === 'vido')
        ? parts.slice(1)
        : parts;
    const relativePath = relativeParts.join('/');

    host.textContent = relativePath || '当前目录';
    host.title = rawPath ? `/data/media/${rawPath}` : '';
})();
