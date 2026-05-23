'use strict';

const { app, BrowserWindow, shell } = require('electron');
const path = require('path');
const { createServer } = require('net');

let serverPort = null;

function findFreePort(port = 3000) {
  return new Promise(resolve => {
    const s = createServer();
    s.listen(port, '127.0.0.1', () => s.close(() => resolve(port)));
    s.on('error', () => findFreePort(port + 1).then(resolve));
  });
}

async function startServer() {
  if (serverPort !== null) return serverPort;

  const port = await findFreePort(3000);

  // User data (sessions.db, .env, client_secret.json) lives next to the exe
  const dataDir = app.isPackaged
    ? path.dirname(app.getPath('exe'))
    : path.join(__dirname);

  // Static files are in app.asar.unpacked when packaged
  const staticDir = app.isPackaged
    ? path.join(process.resourcesPath, 'app.asar.unpacked', 'static')
    : path.join(__dirname, 'static');

  process.env.PORT        = String(port);
  process.env.APP_DATA_DIR = dataDir;
  process.env.STATIC_DIR   = staticDir;

  // Load .env from user data directory before server modules initialize
  require('dotenv').config({ path: path.join(dataDir, '.env') });

  require('./server/index');

  serverPort = port;
  return port;
}

async function createWindow() {
  const port = await startServer();

  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    title: 'MYRAG',
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // Wait briefly for Express to start listening
  await new Promise(r => setTimeout(r, 600));

  win.loadURL(`http://localhost:${port}`);

  // Open external links (e.g. Google OAuth popup) in the system browser
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(`http://localhost:${port}`)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => app.quit());

// macOS re-activation
app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
