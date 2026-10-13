import logging, re
# Redact both key=value forms and common bearer/API token forms; never log raw CCXT exceptions blindly.
PATTERNS=[
 re.compile(r'(?i)(api[_-]?key|api[_-]?secret|access[_-]?key|secret|signature|token|password|authorization)(\s*[=:]\s*)[^\s,;]+'),
 re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*'),
 re.compile(r'\b\d{6,}:[A-Za-z0-9_-]{20,}\b'),
 re.compile(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@'),
]
def redact(value):
 text=str(value)
 for p in PATTERNS:
  text=p.sub(lambda m: (m.group(1) if m.lastindex else '')+'[REDACTED]',text)
 return text
class RedactingFilter(logging.Filter):
 def filter(self,record):
  record.msg=redact(record.getMessage()); record.args=()
  if record.exc_info:
   record.exc_text=redact(logging.Formatter().formatException(record.exc_info)); record.exc_info=None
  if record.exc_text: record.exc_text=redact(record.exc_text)
  return True
def configure_logging(level='INFO'):
 logging.basicConfig(level=getattr(logging,level.upper(),logging.INFO),format='%(asctime)s %(levelname)s %(name)s: %(message)s')
 for h in logging.getLogger().handlers: h.addFilter(RedactingFilter())
