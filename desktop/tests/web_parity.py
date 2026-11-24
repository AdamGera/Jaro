"""Render the shared built UI in Apple's WebKit and the packaged Mac host using a synthetic library.

Run with the project's Python. --reference-only writes light and dark reference images.
--app checks the shipping executable's DOM and exact frontend asset hashes. Add --qa-executable
built with parity_host.m as JARD_TEST_SOURCE to also compare screenshots of the Mac host.
--playback exercises real media and trusted window-local clicks. Supply --qa-executable for
the Mac app and --browser-driver desktop/tests/playback_chromium.cjs for optional Chrome coverage.
The Chrome driver loads playwright-core via ordinary Node resolution (including NODE_PATH).
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import queue
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "engine"))


def seed(root, playback=False):
    from session_store import SessionStore
    from session_notes import publish
    store = SessionStore(root)
    audio = root / "fixture.wav"
    with wave.open(str(audio), "wb") as file:
        file.setparams((1, 2, 16000, 0, "NONE", ""))
        if playback:
            # Two seconds of modulated harmonics, two seconds of digital silence.
            import numpy as np
            t=np.arange(32000)/16000
            voice=(np.sin(2*np.pi*175*t)+.45*np.sin(2*np.pi*351*t))*np.sin(2*np.pi*2*t)**2
            period=(voice*12000).astype("<i2").tobytes()+bytes(64000)
            file.writeframes(period*24)
        else:
            file.writeframes(bytes(96 * 32000))
    options = dict(recording=True, live=True, diarization=True, languageHints=["en"], model="fixture",
                   modelRevision="fixture", speakerModel="fixture", speakerModelRevision="fixture")
    tracks = None
    if playback:
        tracks = {track: root / f"{track}.wav" for track in ("mic", "system")}
        for path in tracks.values():
            shutil.copyfile(audio, path)
    key, _ = store.create(audio, "Design review", "audio/wav", options, 96, tracks=tracks)
    lines = [(1,0,17,"Let’s start with the listening test. People could hear both speakers clearly while wearing headphones."),
             (2,17,35,"The microphone meter helped me check my input before recording. I would keep both meters visible."),
             (1,35,55,"For the next build we will keep the source selector beside Record. The session library and exports stay where they are."),
             (2,55,76,"I’ll try the same setup in a longer call and watch whether the timestamps still line up."),
             (1,76,96,"Good. We will review the recording together on Friday and decide whether the changes are ready.")]
    for speaker, start, end, text in lines:
        words = text.split()
        store.append(key, dict(text=text,language="English",segments=[dict(speaker=speaker,start=start,end=end,text=text)],
            words=[dict(text=w,speaker=speaker,start=start+i*(end-start)/len(words),end=start+(i+1)*(end-start)/len(words),blockStart=start)
                   for i,w in enumerate(words)], activity=[dict(speaker=speaker,start=start,end=end)]), end)

    def complete():
        store.finish(key)
        version = dict(title="Listening Test Review",language="en",
                       summary="The team reviewed headphone recording and agreed on a small change to make microphone and Mac audio levels easier to check.",
                       markdown="## Recording controls\n\nBoth voices were audible through headphones. The team wants to keep **both input meters** visible before recording.\n\n## Next steps\n\n- Keep the source selector beside Record.\n- Check timestamps on a longer call.\n- Review the result together on Friday.",
                       topics=[dict(id=1,start=0,label="Listening test",detail="The team confirmed that both voices were clear through headphones."),
                               dict(id=2,start=35,label="Recording controls",detail="Keep the source selector and both level meters close to Record."),
                               dict(id=3,start=76,label="Friday review",detail="Compare the results of the longer call together.")])
        with store.connection() as db:
            publish(db,key,"en",version)
            db.execute("UPDATE sessions SET created=? WHERE id=?", ("2026-11-19T07:00:00+00:00",key))
            db.execute("""INSERT INTO chat_turns(id,message_id,question,answer,status,source_digest)
                        SELECT id,?,?,?,?,source_digest FROM notes WHERE id=?""",
                       ("visual-smoke","What did we decide?","Keep both input meters visible and test the timing on a longer call." + (" ⟦35⟧" if playback else ""),"complete",key))

    if not playback:
        complete()
    return complete


def contents(dist):
    return {str(path.relative_to(dist)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in dist.rglob("*") if path.is_file() and path.name != ".DS_Store"}


def compare(ref, actual):
    assert ref["assets"] == actual["assets"], "The two clients loaded different React bundles."
    assert ref["viewport"] == actual["viewport"], (ref["viewport"], actual["viewport"])
    assert ref["scheme"] == actual["scheme"], (ref["scheme"],actual["scheme"])
    for screen, elements in ref["frames"].items():
        for name, expected in elements.items():
            item = actual["frames"][screen].get(name)
            assert item is not None, f"{screen}: missing {name}"
            for field, value in expected.items():
                # Native source controls below the dock can adjust the positions of the settings.
                if screen in ("recorder","sessions","sessionSearch","collapsed") and name in ("speakers","hints") and field in ("x","y"):
                    continue
                delta = abs(value - item[field]) if isinstance(value,(int,float)) else float(value != item[field])
                tolerance = 1 if isinstance(value,(int,float)) else 0
                assert delta <= tolerance, f"{screen}/{name}/{field}: browser={value!r} native={item[field]!r}"


def compare_images(folder, reference, native, frames):
    import numpy as np

    def pixels(image):
        bmp=folder / "comparison.bmp"
        # sips decodes both Retina-normalized PNGs in the same macOS color space.
        subprocess.run(["sips","-s","format","bmp",image,"--out",bmp],stdout=subprocess.DEVNULL,check=True)
        data=bmp.read_bytes()
        offset=struct.unpack_from("<I",data,10)[0]
        assert struct.unpack_from("<H",data,28)[0]==32
        return np.frombuffer(data, dtype=np.uint8, offset=offset).reshape(-1,4)[:,:3].astype(np.int16)

    scores={}
    for screen in frames:
        delta=abs(pixels(reference/(screen+".png"))-pixels(native/(screen+".png"))).max(axis=1)
        scores[screen]={"exactPixelFraction":float((delta==0).mean()), "withinThreeLevels":float((delta<=3).mean())}
        # Allow the Mac-specific recorder controls, caret and platform focus/scroll painting.
        assert scores[screen]["withinThreeLevels"]>=.98, f"{screen}: {scores[screen]}"
    (native/"pixel-comparison.json").write_text(json.dumps(scores,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--app",type=Path)
    p.add_argument("--reference-only",action="store_true")
    p.add_argument("--qa-executable",type=Path)
    p.add_argument("--playback",action="store_true",help="Drive trusted local clicks and verify the synthetic audio, playhead, viewer and waveform")
    p.add_argument("--browser-driver",type=Path,help="Optional browser driver executable reading JARD_QA_URL and serving trusted test clicks")
    p.add_argument("--out",type=Path,default=REPO/"desktop/out/parity-system")
    args=p.parse_args()
    if not args.app and not args.reference_only:
        p.error("Choose --app or --reference-only")
    out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jard-webkit-parity-") as scratch:
        scratch=Path(scratch)
        library=scratch/"library"
        sock=socket.socket()
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
        base=f"http://127.0.0.1:{port}"
        os.environ.update(RUMBLE_DATA_DIR=str(library),RUMBLE_LOG_DIR=str(scratch/"logs"),RUMBLE_PORT=str(port))
        from fastapi import HTTPException, Request
        from fastapi.responses import Response
        from fastapi.staticfiles import StaticFiles
        import uvicorn
        import server
        complete_session=None
        completed=False
        dist=REPO/"dist"
        app_binary=None
        debug_binary=args.qa_executable.resolve() if args.qa_executable else None
        # A source-host comparison can run before the signed package is assembled.
        # The release host ignores these two QA-only environment entries.
        qa_helper=scratch/"jard-ui"
        qa_helper.write_text("#!/bin/sh\nexec "+shlex.join([sys.executable,"-u",str(REPO/"desktop/mac_bridge.py")])+"\n")
        qa_helper.chmod(0o700)
        qa_capture=scratch/"jard-capture"
        qa_capture.write_text("#!/bin/sh\nprintf '%s\\n' '{\"microphone\":\"Fixture microphone\"}'\n")
        qa_capture.chmod(0o700)
        if args.app:
            app=args.app.resolve()
            with (app/"Contents/Info.plist").open("rb") as f:
                executable=plistlib.load(f)["CFBundleExecutable"]
            app_binary=app/"Contents/MacOS"/executable
            packaged=app/"Contents/Resources/engine/dist"
            assert contents(dist)==contents(packaged),"Packaged and source dist differ. Rebuild before comparing."
            dist=packaged
            if args.qa_executable:
                qa=scratch/"QA.app"
                subprocess.run(["/bin/cp","-cR",app,qa],check=True)
                debug_binary=qa/"Contents/MacOS"/executable
                shutil.copy2(args.qa_executable,debug_binary)
                subprocess.run(["codesign","--force","--sign","-",qa],check=True)
        test_dist=scratch/"dist"
        shutil.copytree(dist,test_dist)
        token=secrets.token_hex(32)
        index=test_dist/"index.html"
        index.write_text(index.read_text().replace("</body>", f'<script defer src="/__parity/test.js?token={token}"></script></body>'))
        mode={}
        reports=queue.Queue()
        def check(req):
            if req.headers.get("x-parity-token")!=token:
                raise HTTPException(403)
        @server.app.get("/__parity/test.js")
        def script():
            return Response((HERE/("playback_flow.js" if args.playback else "parity_flow.js")).read_bytes(),media_type="text/javascript")
        @server.app.get("/__parity/config")
        def config(request: Request):
            check(request)
            return mode
        @server.app.post("/__parity/complete")
        def complete_fixture(request: Request):
            nonlocal completed
            check(request)
            if not completed:
                complete_session()
                completed=True
            return {"ok":True}
        @server.app.post("/__parity/report")
        async def report(request: Request):
            check(request)
            reports.put(await request.json())
            return {"ok":True}
        async def ready(app):
            nonlocal complete_session
            # Fixtures exercise real persistence and presentation without a microphone or model downloads.
            complete_session=seed(library, playback=args.playback)
            app.state.status="ready"
            app.state.diarizer=object()
        server.load_model=ready
        server.DIST=test_dist
        server.app.mount("/",StaticFiles(directory=test_dist,html=True))
        runner=uvicorn.Server(uvicorn.Config(server.app,access_log=False,log_level="warning",timeout_graceful_shutdown=1))
        thread=threading.Thread(target=runner.run,kwargs={"sockets":[sock]},daemon=True)
        thread.start()
        ref=scratch/"reference"
        subprocess.run(["xcrun","clang","-fobjc-arc","-DJARD_REFERENCE","-framework","Cocoa","-framework","WebKit",
                        HERE/"parity_host.m","-o",ref],check=True)
        def render(binary, label, scheme, size, snapshots, native_layout=None):
            directory=out/(label+"-"+scheme)
            directory.mkdir(exist_ok=True)
            env={**os.environ,"JARD_QA_URL":base,"JARD_QA_SCHEME":scheme,"JARD_QA_WIDTH":str(size[0]),
                 "JARD_QA_HEIGHT":str(size[1]),"JARD_QA_PNG_DIR":str(directory) if snapshots else ""}
            if args.playback:
                env.update(JARD_QA_PLAYBACK="1",JARD_QA_PNG_DIR=str(directory))
            if label=="native" and not args.app:
                env.update(JARD_TEST_HELPER=str(qa_helper), JARD_TEST_SETUP=str(REPO/"desktop/setup.html"))
            mode.update(snapshots=snapshots, nativeLayout=native_layout, checkPermissions=label=="production")
            with (directory/"host.log").open("w") as log, subprocess.Popen(
                ([str(binary)] if scheme=="system" or args.playback else [str(binary),"-AppleInterfaceStyle",scheme.capitalize()]),env=env,stdout=log,stderr=log) as proc:
                try:
                    deadline=time.monotonic()+75
                    while proc.poll() is None and time.monotonic()<deadline:
                        with contextlib.suppress(queue.Empty):
                            data=reports.get(timeout=.25)
                            assert "error" not in data, data
                            assert data["desktop"] == (label in ("native", "production")),"The shipping JS bridge did not match this host."
                            if args.playback:
                                assert "afterCompletion" in data["frames"], "Playback did not exercise session completion."
                            (directory/"dom.json").write_text(json.dumps(data,indent=2))
                            if snapshots:
                                assert all((directory/(s+".png")).is_file() for s in data["frames"])
                            print(label,scheme,data["viewport"],list(data["frames"]),flush=True)
                            return data
                    raise RuntimeError(f"{label} {scheme} did not report. See {directory/'host.log'}.")
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try: proc.wait(timeout=5)
                        except subprocess.TimeoutExpired: proc.kill()
        try:
            # The shipping binary follows the Mac's selected appearance. Only the
            # screenshot observer sets a QA appearance explicitly for both themes.
            size=(1050,760)
            if args.playback:
                render(ref,"reference","playback",size,False)
                for binary, label in ((debug_binary, "native"), (args.browser_driver, "browser")):
                    if binary:
                        complete_session = seed(library, playback=True)
                        completed = False
                        render(binary.resolve(),label,"playback",size,False)
                return
            actual=render(app_binary,"production","system",size,False) if app_binary else None
            if actual:
                size=actual["viewport"]
            for scheme in ["light","dark"]:
                browser=render(ref,"reference",scheme,size,True)
                target=render(debug_binary,"native",scheme,size,True) if debug_binary else None
                production=actual if actual and actual["scheme"]==scheme else None
                layout=(target or production or {}).get("nativeLayout")
                # Compare against the same CSS geometry around the Mac window controls.
                # The plain browser baseline separately exercises ordinary browser behavior.
                expected = render(ref,"mac-layout",scheme,size,True,layout) if layout else browser
                if production:
                    compare(expected,production)
                if target:
                    compare(expected,target)
                    expected_dir=("mac-layout-" if layout else "reference-")+scheme
                    compare_images(scratch,out/expected_dir,out/("native-"+scheme),target["frames"])
        finally:
            runner.should_exit=True
            thread.join(4)
            sock.close()


if __name__=="__main__":
    main()
