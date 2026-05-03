# telegramtotxt

A utility for converting Telegram chat exports into plain text files.

## Overview

`telegramtotxt` takes Telegram chat exports (typically JSON or HTML produced by Telegram Desktop's "Export chat history" feature) and converts them into clean, readable `.txt` files suitable for archiving, searching, or further processing.

## Features

- Convert Telegram exports to plain text
- Preserve message timestamps and sender names
- Handle edited and forwarded messages
- Skip or annotate media attachments

## Usage

```bash
telegramtotxt <input-export> -o <output.txt>
```

## Requirements

- A Telegram chat export from Telegram Desktop

## License

See repository for license details.
