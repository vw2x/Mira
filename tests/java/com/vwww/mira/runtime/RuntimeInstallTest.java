package com.vwww.mira.runtime;

import android.content.Context;
import android.content.res.AssetManager;
import java.io.*;
import java.nio.file.*;
import java.util.zip.*;

public final class RuntimeInstallTest {
    static final String PYTHON = "lib/python3.14";
    static final String FRIDA = PYTHON + "/site-packages/_frida.abi3.so";
    static final String NESTED_FRIDA = PYTHON + "/site-packages/frida/_frida.abi3.so";
    static final String PAYLOAD = "unique-runtime-payload-for-crc-test";
    static void zip(File file) throws Exception {
        zip(file, false);
    }
    static void zip(File file, boolean nested) throws Exception {
        String extension = nested ? NESTED_FRIDA : FRIDA;
        try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(file))) {
            for (String name : new String[] {extension, "bin/python3", "bin/pip", "bin/frida-official",
                "bin/mira-frida-agent.py",
                PYTHON + "/site-packages/frida/__init__.py",
                PYTHON + "/site-packages/frida_tools/application.py",
                PYTHON + "/site-packages/colorama/__init__.py",
                PYTHON + "/site-packages/prompt_toolkit/__init__.py",
                PYTHON + "/site-packages/pygments/__init__.py",
                PYTHON + "/site-packages/wcwidth/__init__.py",
                PYTHON + "/site-packages/pip/__init__.py",
                PYTHON + "/zipfile/_path/__init__.py"}) {
                byte[] data = (name.equals(extension) ? PAYLOAD : "content").getBytes("UTF-8");
                CRC32 crc = new CRC32(); crc.update(data);
                ZipEntry entry = new ZipEntry("assets/bootstrap/prefix/arm64-v8a/" + name);
                entry.setMethod(ZipEntry.STORED); entry.setSize(data.length); entry.setCrc(crc.getValue());
                out.putNextEntry(entry); out.write(data); out.closeEntry();
            }
            if (nested) {
                out.putNextEntry(new ZipEntry("assets/bootstrap/prefix/arm64-v8a/" + PYTHON + "/site-packages/_frida.py"));
                out.write("compatibility alias".getBytes("UTF-8"));
                out.closeEntry();
            }
        }
    }
    static void check(boolean value) { if (!value) throw new AssertionError(); }
    static void restartProcess() throws Exception {
        java.lang.reflect.Field completed = RuntimeInstaller.class.getDeclaredField("INSTALL_COMPLETED");
        completed.setAccessible(true);
        ((java.util.concurrent.atomic.AtomicBoolean) completed.get(null)).set(false);
    }
    public static void main(String[] args) throws Exception {
        File root = new File(args[0]); root.mkdirs();
        File apk = new File(root, "test.apk"); zip(apk);
        byte[] bytes = Files.readAllBytes(apk.toPath());
        byte[] match = PAYLOAD.getBytes("UTF-8");
        int offset = -1;
        outer: for (int i = 0; i <= bytes.length - match.length; i++) {
            for (int j = 0; j < match.length; j++) if (bytes[i+j] != match[j]) continue outer;
            offset = i; break;
        }
        check(offset >= 0); bytes[offset] ^= 1; Files.write(apk.toPath(), bytes);
        File files = new File(root, "files"); files.mkdirs();
        File home = new File(files, "home"); home.mkdirs();
        File profile = new File(home, ".profile"); Files.write(profile.toPath(), "user profile".getBytes());
        File old = new File(files, "usr/" + FRIDA); old.getParentFile().mkdirs();
        Files.write(old.toPath(), "previous file".getBytes());
        File customShell = new File(files, "usr/bin/sh"); customShell.getParentFile().mkdirs();
        Files.write(customShell.toPath(), "#!/system/bin/sh\necho user shell\n".getBytes());
        File state = new File(files, ".mira-bootstrap-state");
        Files.write(state.toPath(), "# Managed by MiraBootstrap\nversion=8\n".getBytes());
        Context context = new Context() {
            public Context getApplicationContext() { return this; }
            public AssetManager getAssets() { return new AssetManager(); }
            public File getFilesDir() { return files; }
            public File getCacheDir() { return new File(root, "cache"); }
            public String getPackageCodePath() { return apk.getAbsolutePath(); }
        };
        RuntimeInstaller installer = new RuntimeInstaller(context);
        boolean failed = false;
        try { installer.installIfNeeded(); } catch (IOException expected) { failed = true; }
        check(failed); check(!state.exists());
        check(new String(Files.readAllBytes(old.toPath())).equals("previous file"));
        zip(apk); installer.installIfNeeded();
        check(state.isFile());
        check(new String(Files.readAllBytes(old.toPath())).equals(PAYLOAD));
        check(new String(Files.readAllBytes(customShell.toPath())).equals("#!/system/bin/sh\necho user shell\n"));
        check(new String(Files.readAllBytes(profile.toPath())).equals("user profile"));
        check(!new File(files, ".mira-extract-.mira-bootstrap-state").exists());
        File userModule = new File(files, "usr/" + PYTHON + "/site-packages/user_module.py");
        Files.write(userModule.toPath(), "user installed package".getBytes());
        for (String name : new String[] {"colorama", "prompt_toolkit", "pygments", "wcwidth"}) {
            File dependency = new File(files, "usr/" + PYTHON + "/site-packages/" + name + "/__init__.py");
            for (boolean truncate : new boolean[] {false, true}) {
                if (truncate) Files.write(dependency.toPath(), new byte[0]);
                else Files.delete(dependency.toPath());
                restartProcess();
                installer.installIfNeeded();
                check(dependency.isFile());
                check(new String(Files.readAllBytes(dependency.toPath())).equals("content"));
                check(new String(Files.readAllBytes(userModule.toPath())).equals("user installed package"));
                check(new String(Files.readAllBytes(profile.toPath())).equals("user profile"));
                check(new String(Files.readAllBytes(customShell.toPath())).equals("#!/system/bin/sh\necho user shell\n"));
            }
        }
        // A complete current runtime must not require APK extraction again.
        Files.delete(apk.toPath());
        restartProcess();
        installer.installIfNeeded();
        // Nested bindings require the alias even when an old top-level binary remains.
        zip(apk, true);
        File nested = new File(files, "usr/" + NESTED_FRIDA);
        Files.move(old.toPath(), nested.toPath());
        Files.write(old.toPath(), "stale legacy binary".getBytes());
        File alias = new File(files, "usr/" + PYTHON + "/site-packages/_frida.py");
        check(!alias.exists());
        for (boolean truncate : new boolean[] {false, true}) {
            if (truncate) Files.write(alias.toPath(), new byte[0]);
            restartProcess();
            installer.installIfNeeded();
            check(new String(Files.readAllBytes(alias.toPath())).equals("compatibility alias"));
            check(new String(Files.readAllBytes(userModule.toPath())).equals("user installed package"));
            check(new String(Files.readAllBytes(profile.toPath())).equals("user profile"));
        }
        Files.delete(apk.toPath());
        restartProcess();
        installer.installIfNeeded();
        System.out.println("PASS: actual runtime install recovers failures and current-but-incomplete dependencies, preserves user files, skips complete runtime");
    }
}
