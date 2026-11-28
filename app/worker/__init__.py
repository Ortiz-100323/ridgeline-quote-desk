"""Worker: turns one queued RFQ into a draft quote. Run: python -m app.worker

worker.Worker: claim -> transformer.transform() -> extractor.Extractor.extract()
-> matcher.Matcher.match_line() -> quotes / quote_lines / outputs JSON -> job update.
"""
