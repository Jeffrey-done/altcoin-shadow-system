/* ═══════════════════════════════════════════════════════════════
   Shadow Trading System - Binance WebSocket Manager
   Real-time price feed from Binance miniTicker streams
   ═══════════════════════════════════════════════════════════════ */

const BinanceWS = {
    ws: null,
    currentSymbols: [],
    prices: {},
    reconnectTimer: null,
    lastData: null,
    onPriceUpdate: null, // callback(symbol, price)

    updateSymbols(symbols) {
        const sorted = [...symbols].sort().join(',');
        const current = [...this.currentSymbols].sort().join(',');
        if (sorted === current && this.ws && this.ws.readyState === WebSocket.OPEN) return;
        this.currentSymbols = [...symbols];
        this.connect();
    },

    connect() {
        // Close old connection
        if (this.ws) {
            this.ws.onclose = null;
            this.ws.close();
            this.ws = null;
        }
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        if (this.currentSymbols.length === 0) return;

        // Build stream names
        const streams = this.currentSymbols.map(sym => {
            const binSym = sym.replace('/USDT', 'usdt').replace('/', '').toLowerCase();
            return binSym + '@miniTicker';
        });

        const url = 'wss://stream.binance.com:9443/stream?streams=' + streams.join('/');
        try {
            this.ws = new WebSocket(url);
        } catch (e) {
            console.warn('[BinanceWS] Connection failed:', e);
            return;
        }

        this.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                const data = msg.data;
                if (!data || !data.s || !data.c) return;

                const binSym = data.s;
                const price = parseFloat(data.c);

                // Convert back to ccxt format
                const ccxtSym = this.currentSymbols.find(s =>
                    s.replace('/USDT', 'USDT').replace('/', '') === binSym
                );

                if (ccxtSym && price > 0) {
                    this.prices[ccxtSym] = price;
                    if (this.onPriceUpdate) {
                        this.onPriceUpdate(ccxtSym, price);
                    }
                }
            } catch (e) { /* ignore parse errors */ }
        };

        this.ws.onclose = () => {
            // Auto-reconnect after 5 seconds
            this.reconnectTimer = setTimeout(() => this.connect(), 5000);
        };

        this.ws.onerror = () => { /* onclose will handle reconnect */ };
    },

    disconnect() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        if (this.ws) {
            this.ws.onclose = null;
            this.ws.close();
            this.ws = null;
        }
    },

    getPrice(symbol) {
        return this.prices[symbol] || null;
    }
};
