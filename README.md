# YouTube Channel Searcher and Playlist Maker

A local-only web app that indexes one YouTube channel's public video metadata with the official YouTube Data API, then lets you search that channel by species name, common name, scientific name, or any other keyword.

It is built for the annoying moment when you know a channel talked about dolphins, hyenas, milkweed, or some obscure species, but YouTube's own channel search is not helping.

## What It Does

- Imports public metadata from one YouTube channel.
- Searches video titles, descriptions, and tags locally.
- Shows clickable results with thumbnails, dates, snippets, and YouTube links.
- Creates a temporary playlist-style YouTube queue from any search term.
- Runs in dark mode.
- Shuts itself down after an idle timeout.
- Keeps your indexed data on your own machine.

## Privacy and Legality

This app is intentionally conservative:

- It uses the official YouTube Data API.
- It does not scrape YouTube pages.
- It does not download videos.
- It does not fetch captions or transcripts.
- It does not require access to your YouTube account.
- It stores the API key in a local `.env` file that should never be committed.

Each user should create and use their own YouTube Data API key.

## Quick Start

Create a `.env` file:

```bash
cp .env.example .env
```

Add your YouTube Data API key:

```bash
YOUTUBE_API_KEY=your_key_here
DATABASE_PATH=./data/youtube_species.db
IDLE_SHUTDOWN_SECONDS=30
```

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run the app locally:

```bash
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## Basic Workflow

1. Paste a YouTube channel URL.
2. Click **Import Channel**.
3. Search for a species name or keyword.
4. Click a video result, or click **Create Playlist** to build a temporary queue from the search results.

## Security Notes

Run the app on localhost:

```bash
--host 127.0.0.1
```

Avoid exposing it to your network with `--host 0.0.0.0`.

The app auto-shuts down after `IDLE_SHUTDOWN_SECONDS` with no browser activity. Set it to `0` to disable idle shutdown.

## Project Status

Early personal-tool prototype. Useful today, intentionally small, and designed to stay local-first.

## Not Affiliated

This project is not affiliated with YouTube or Google.
