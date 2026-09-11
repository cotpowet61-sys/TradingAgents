let ws;
const agentsLog = document.getElementById('agents-log');
const coinsList = document.getElementById('coins-list');
const tickerData = document.getElementById('ticker-data');

function connectWebSocket() {
    ws = new WebSocket(`ws://${window.location.hostname}:3001/ws`);
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'log') {
            addLog(data.agent, data.message);
        }
    };
    
    ws.onclose = () => {
        addLog('System', 'Connection lost. Reconnecting...');
        setTimeout(connectWebSocket, 3000);
    };
}

function addLog(agent, message) {
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `<strong>[${time}] ${agent}:</strong> ${message}`;
    agentsLog.insertBefore(entry, agentsLog.firstChild);
    if (agentsLog.children.length > 50) agentsLog.lastChild.remove();
}

// TradingView Widget
new TradingView.widget({
    "container_id": "tradingview-chart",
    "autosize": true,
    "symbol": "BYBIT:BTCUSDT",
    "interval": "5",
    "timezone": "Etc/UTC",
    "theme": "dark",
    "style": "1",
    "locale": "en",
    "toolbar_bg": "#0d0208",
    "enable_publishing": false,
    "hide_side_toolbar": false,
    "allow_symbol_change": true
});

connectWebSocket();
addLog('System', 'Interface loaded. Connecting to backend...');
