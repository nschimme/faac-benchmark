import sys, io, contextlib; sys.path.insert(0,'/Users/nschimme/gitprojects/faac-benchmark/scripts'); sys.path.insert(0,'/Users/nschimme/gitprojects/faac-benchmark')
from score_clip import score_clip
with contextlib.redirect_stdout(io.StringIO()): r = score_clip(sys.argv[1], sys.argv[2])
print(r[0] if isinstance(r,tuple) else r)
