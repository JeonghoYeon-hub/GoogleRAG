'use strict';

// Shared mutable state — single source of truth for in-memory server/file data.
// Node.js module cache ensures this object is a singleton across all requires.
const state = {
  servers: {},   // { [serverId]: { alias, store_name, api_key, model, files: [...] } }
  apiKeys: {
    gemini: process.env.GOOGLE_API_KEY || '',
    notion: process.env.NOTION_API_KEY || '',
  },
};

module.exports = state;
