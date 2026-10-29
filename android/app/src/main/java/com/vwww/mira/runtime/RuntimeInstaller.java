package com.vwww.mira.runtime;

import com.vwww.mira.BuildConfig;

import android.content.Context;
import android.content.res.AssetManager;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Enumeration;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Installs Mira's APK-bundled runtime and managed shell scripts into the app's
 * private files directory.
 */
public final class RuntimeInstaller {
    private static final String TAG = "MiraBootstrap";
    private static final String BOOTSTRAP_PREFIX_ASSET_ROOT = "bootstrap/prefix";
    private static final String INSTALL_STATE_FILE_NAME = ".mira-bootstrap-state";
    private static final int INSTALL_STATE_VERSION = BuildConfig.MIRA_BOOTSTRAP_STATE_VERSION;
    private static final AtomicBoolean INSTALL_COMPLETED = new AtomicBoolean(false);
    private static final Object INSTALL_LOCK = new Object();

    private final File filesDir;
    private final File prefixDir;
    private final File homeDir;
    private final File tmpDir;
    private final File installStateFile;
    private final AssetManager assets;
    private final String packageCodePath;
    private final RuntimeScripts scripts;

    public RuntimeInstaller(Context context) {
        Context appContext = context.getApplicationContext();
        assets = appContext.getAssets();
        filesDir = appContext.getFilesDir();
        prefixDir = new File(filesDir, "usr");
        homeDir = new File(filesDir, "home");
        tmpDir = new File(appContext.getCacheDir(), "tmp");
        installStateFile = new File(filesDir, INSTALL_STATE_FILE_NAME);
        packageCodePath = appContext.getPackageCodePath();
        scripts = new RuntimeScripts(prefixDir, homeDir, tmpDir);
    }

    public void installIfNeeded() throws IOException {
        if (INSTALL_COMPLETED.get()) {
            ensureRuntimeDirectories();
            return;
        }
        synchronized (INSTALL_LOCK) {
            if (INSTALL_COMPLETED.get()) {
                ensureRuntimeDirectories();
                return;
            }
            if (isBootstrapCurrent()) {
                ensureRuntimeDirectories();
                INSTALL_COMPLETED.set(true);
                Log.i(TAG, "Bootstrap already installed, skip reinstall");
                return;
            }
            long startedAt = SystemClock.elapsedRealtime();
            Log.i(TAG, "Bootstrap install begin prefix=" + prefixDir.getAbsolutePath());
            try {
                // Invalidate a stale marker before changing any runtime files.
                if (installStateFile.exists() && !installStateFile.delete()) {
                    throw new IOException("Cannot invalidate bootstrap state: " + installStateFile);
                }
                ensureRuntimeDirectories();
                installBootstrapPrefixIfAvailable();
                mkdir(new File(prefixDir, "bin"));
                deleteStaleShellRuntimeFiles(new File(prefixDir, "bin"));
                mkdir(new File(prefixDir, "etc"));
                mkdir(new File(prefixDir, "etc/profile.d"));
                mkdir(new File(prefixDir, "tmp"));
                mkdir(new File(prefixDir, "var"));
                mkdir(new File(prefixDir, "share"));

                writeManagedExecutable(new File(prefixDir, "bin/sh"), scripts.shellWrapper(), "export MIRA_SANDBOX=1", "export ENV=\"$HOME/.profile\"");
                writeManagedExecutable(new File(prefixDir, "bin/mira-info"), scripts.miraInfoScript(), "echo \"Mira sandbox\"");
                writeManagedExecutable(new File(prefixDir, "bin/frida"), scripts.fridaWrapperScript());
                writeMiraCommandWrappers();
                writeManagedText(new File(prefixDir, "etc/profile.d/mira-env.sh"), scripts.profileHookScript());
                writeManagedText(new File(prefixDir, "etc/profile"), scripts.profileScript(), "# Mira minimal profile");
                writeManagedText(new File(homeDir, ".profile"), scripts.homeProfileScript(), "# Mira shell profile");
                writeInstallState();
                INSTALL_COMPLETED.set(true);
                Log.i(TAG, "Bootstrap install complete in " + (SystemClock.elapsedRealtime() - startedAt) + "ms");
            } catch (IOException | RuntimeException e) {
                Log.w(TAG, "Bootstrap install failed after " + (SystemClock.elapsedRealtime() - startedAt) + "ms: " + e.getMessage(), e);
                throw e;
            }
        }
    }

    public File getPrefixDir() {
        return prefixDir;
    }

    public File getHomeDir() {
        return homeDir;
    }

    public File getTmpDir() {
        return tmpDir;
    }

    public File getShellPath() {
        return new File(prefixDir, "bin/sh");
    }

    private void mkdir(File directory) throws IOException {
        if (directory.isDirectory()) return;
        if (!directory.mkdirs() && !directory.isDirectory()) {
            throw new IOException("无法创建目录: " + directory.getAbsolutePath());
        }
    }

    private void ensureRuntimeDirectories() throws IOException {
        mkdir(prefixDir);
        mkdir(homeDir);
        mkdir(tmpDir);
    }

    private boolean isBootstrapCurrent() throws IOException {
        if (!installStateFile.isFile()) return false;
        String state = readText(installStateFile);
        if (!state.equals(RuntimeScripts.MANAGED_MARKER + "\nversion=" + INSTALL_STATE_VERSION + "\n")) return false;
        return prefixDir.isDirectory()
            && homeDir.isDirectory()
            && new File(prefixDir, "bin/sh").isFile()
            && new File(prefixDir, "bin/frida").isFile()
            && new File(prefixDir, "bin/python3").isFile()
            && new File(prefixDir, "bin/frida-official").isFile()
            && new File(prefixDir, "bin/mira-frida-agent.py").isFile()
            && hasCompletePythonRuntime()
            && new File(prefixDir, "etc/profile").isFile()
            && new File(homeDir, ".profile").isFile();
    }

    private boolean hasCompletePythonRuntime() {
        String[] requiredFiles = {
            "site-packages/frida/__init__.py",
            "site-packages/frida_tools/application.py",
            "site-packages/pip/__init__.py",
            "site-packages/colorama/__init__.py",
            "site-packages/prompt_toolkit/__init__.py",
            "site-packages/pygments/__init__.py",
            "site-packages/wcwidth/__init__.py",
            "zipfile/_path/__init__.py"
        };
        File libDir = new File(prefixDir, "lib");
        File[] candidates = libDir.listFiles(file -> file.isDirectory()
            && file.getName().matches("python\\d+\\.\\d+"));
        if (candidates == null) return false;
        for (File pythonDir : candidates) {
            File sitePackages = new File(pythonDir, "site-packages");
            boolean hasExtension = new File(sitePackages, "_frida.abi3.so").isFile()
                || new File(sitePackages, "frida/_frida.abi3.so").isFile();
            if (!hasExtension) continue;
            if (new File(sitePackages, "frida/_frida.abi3.so").isFile()) {
                File alias = new File(sitePackages, "_frida.py");
                if (!alias.isFile() || alias.length() == 0) continue;
            }
            boolean complete = true;
            for (String relative : requiredFiles) {
                File file = new File(pythonDir, relative);
                if (!file.isFile() || file.length() == 0) {
                    complete = false;
                    break;
                }
            }
            if (complete) return true;
        }
        return false;
    }

    private void writeInstallState() throws IOException {
        byte[] state = (RuntimeScripts.MANAGED_MARKER + "\nversion=" + INSTALL_STATE_VERSION + "\n").getBytes(StandardCharsets.UTF_8);
        CRC32 crc = new CRC32();
        crc.update(state);
        VerifiedExtraction.extract(() -> new ByteArrayInputStream(state),
            installStateFile, state.length, crc.getValue(), false);
    }

    private void writeExecutable(File file, String content) throws IOException {
        writeText(file, content);
        if (!file.setReadable(true, true)) throw new IOException("无法设置可读权限: " + file.getAbsolutePath());
        if (!file.setWritable(true, true)) throw new IOException("无法设置可写权限: " + file.getAbsolutePath());
        if (!file.setExecutable(true, true)) throw new IOException("无法设置可执行权限: " + file.getAbsolutePath());
    }

    private void writeManagedExecutable(File file, String content, String... legacyMarkers) throws IOException {
        if (!shouldWriteManagedFile(file, legacyMarkers)) return;
        writeExecutable(file, content);
    }

    private void writeManagedText(File file, String content, String... legacyMarkers) throws IOException {
        if (!shouldWriteManagedFile(file, legacyMarkers)) return;
        writeText(file, content);
    }

    private void writeText(File file, String content) throws IOException {
        File parent = file.getParentFile();
        if (parent != null) mkdir(parent);
        try (FileOutputStream output = new FileOutputStream(file, false)) {
            output.write(content.getBytes(StandardCharsets.UTF_8));
        }
    }

    private boolean shouldWriteManagedFile(File file, String... legacyMarkers) throws IOException {
        if (!file.exists()) return true;
        String existing = readText(file);
        if (existing.contains(RuntimeScripts.MANAGED_MARKER)) return true;
        if (legacyMarkers != null) {
            for (String marker : legacyMarkers) {
                if (marker != null && !marker.isEmpty() && existing.contains(marker)) return true;
            }
        }
        Log.i(TAG, "Preserve existing non-managed file " + file.getAbsolutePath());
        return false;
    }

    private String readText(File file) throws IOException {
        byte[] buffer = new byte[(int) file.length()];
        try (FileInputStream input = new FileInputStream(file)) {
            int offset = 0;
            while (offset < buffer.length) {
                int read = input.read(buffer, offset, buffer.length - offset);
                if (read < 0) break;
                offset += read;
            }
            return new String(buffer, 0, offset, StandardCharsets.UTF_8);
        }
    }

    private void writeMiraCommandWrappers() throws IOException {
        String[] commands = new String[] {
            "am",
            "mira-am",
            "mira-settings",
            "mira-getprop",
            "mira-dumpsys",
            "mira-logcat"
        };
        File binDir = new File(prefixDir, "bin");
        deleteLegacyFridaWrappers(binDir);
        for (String command : commands) {
            writeExecutable(new File(binDir, command), scripts.miraCommandScript(command));
        }
    }

    private void deleteStaleShellRuntimeFiles(File binDir) throws IOException {
        String[] staleFiles = new String[] {
            "bash",
            "bash.bin",
            "coreutils",
            "coreutils.bin",
            "dirname",
            "dirname.bin",
            "find",
            "find.bin",
            "grep",
            "grep.bin",
            "ls",
            "ls.bin"
        };
        for (String name : staleFiles) {
            File file = new File(binDir, name);
            if (file.exists() && !file.delete()) {
                throw new IOException("无法删除旧版 shell runtime 文件: " + file.getAbsolutePath());
            }
        }
    }

    private void deleteLegacyFridaWrappers(File binDir) throws IOException {
        String[] legacyCommands = new String[] {
            "frida-status",
            "frida-exec",
            "frida-load",
            "frida-hook",
            "frida-call",
            "frida-native-hook",
            "frida-detach"
        };
        for (String command : legacyCommands) {
            File wrapper = new File(binDir, command);
            if (wrapper.exists() && !wrapper.delete()) {
                throw new IOException("无法删除旧版 Frida wrapper: " + wrapper.getAbsolutePath());
            }
        }
    }

    private void installBootstrapPrefixIfAvailable() throws IOException {
        String assetRoot = selectBootstrapPrefixAssetRoot();
        if (assetRoot == null) {
            Log.i(TAG, "No packaged bootstrap prefix for current ABI, keeping minimal prefix");
            return;
        }
        extractBootstrapPrefixFromApk(assetRoot, prefixDir);
        Log.i(TAG, "Installed bootstrap prefix assets from " + assetRoot);
    }

    private void extractBootstrapPrefixFromApk(String assetRoot, File destinationRoot) throws IOException {
        String prefix = "assets/" + assetRoot + "/";
        try (ZipFile zipFile = new ZipFile(packageCodePath)) {
            for (String required : new String[] {
                "bin/python3", "bin/pip", "bin/frida-official", "bin/mira-frida-agent.py"
            }) {
                ZipEntry entry = zipFile.getEntry(prefix + required);
                if (entry == null || entry.isDirectory() || entry.getSize() <= 0) {
                    throw new IOException("Missing or empty bootstrap entry: " + prefix + required);
                }
            }
            String pythonRoot = findBootstrapPythonRoot(zipFile, prefix);
            ZipEntry topLevelFrida = zipFile.getEntry(prefix
                + pythonRoot + "/site-packages/_frida.abi3.so");
            ZipEntry packagedFrida = zipFile.getEntry(prefix
                + pythonRoot + "/site-packages/frida/_frida.abi3.so");
            if ((topLevelFrida == null || topLevelFrida.isDirectory() || topLevelFrida.getSize() <= 0)
                && (packagedFrida == null || packagedFrida.isDirectory() || packagedFrida.getSize() <= 0)) {
                throw new IOException("Missing or empty Frida extension in " + prefix);
            }
            Enumeration<? extends ZipEntry> entries = zipFile.entries();
            boolean extractedAny = false;
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                String name = entry.getName();
                if (!name.startsWith(prefix) || entry.isDirectory()) continue;
                String relativePath = name.substring(prefix.length());
                File targetFile = safeDestinationFile(destinationRoot, relativePath);
                VerifiedExtraction.extract(() -> zipFile.getInputStream(entry),
                    targetFile, entry.getSize(), entry.getCrc(), shouldBeExecutable(relativePath));
                extractedAny = true;
            }
            if (extractedAny) return;
        }
        throw new IOException("No bootstrap ZIP entries found for " + assetRoot);
    }

    private String findBootstrapPythonRoot(ZipFile zipFile, String prefix) throws IOException {
        Enumeration<? extends ZipEntry> entries = zipFile.entries();
        while (entries.hasMoreElements()) {
            String name = entries.nextElement().getName();
            if (!name.startsWith(prefix + "lib/python")
                || !name.endsWith("/site-packages/frida/__init__.py")) continue;
            String relative = name.substring(prefix.length());
            int separator = relative.indexOf("/site-packages/");
            String pythonRoot = separator >= 0 ? relative.substring(0, separator) : "";
            if (!pythonRoot.matches("lib/python\\d+\\.\\d+")) continue;
            for (String required : new String[] {
                pythonRoot + "/site-packages/pip/__init__.py",
                pythonRoot + "/zipfile/_path/__init__.py"
            }) {
                ZipEntry entry = zipFile.getEntry(prefix + required);
                if (entry == null || entry.isDirectory()) {
                    throw new IOException("Missing bootstrap entry: " + prefix + required);
                }
            }
            return pythonRoot;
        }
        throw new IOException("Missing Python runtime under " + prefix);
    }

    private File safeDestinationFile(File destinationRoot, String relativePath) throws IOException {
        if (relativePath == null || relativePath.isEmpty() || relativePath.startsWith("/") || relativePath.contains("\\")) {
            throw new IOException("Unsafe bootstrap entry path: " + relativePath);
        }
        File root = destinationRoot.getCanonicalFile();
        File target = root;
        for (String part : relativePath.split("/")) {
            if (part.isEmpty() || ".".equals(part) || "..".equals(part)) {
                throw new IOException("Unsafe bootstrap entry path: " + relativePath);
            }
            target = new File(target, part);
        }
        File canonicalTarget = target.getCanonicalFile();
        String rootPath = root.getPath();
        String targetPath = canonicalTarget.getPath();
        if (!targetPath.equals(rootPath) && !targetPath.startsWith(rootPath + File.separator)) {
            throw new IOException("Bootstrap entry escapes destination: " + relativePath);
        }
        return canonicalTarget;
    }

    private String selectBootstrapPrefixAssetRoot() throws IOException {
        String[] abis = Build.SUPPORTED_ABIS == null ? new String[0] : Build.SUPPORTED_ABIS;
        for (String abi : abis) {
            String assetRoot = bootstrapPrefixAssetRootForAbi(abi);
            if (assetRoot != null && assetDirectoryExists(assetRoot)) return assetRoot;
        }
        return null;
    }

    private String bootstrapPrefixAssetRootForAbi(String abi) {
        if (abi == null) return null;
        String normalized = abi.toLowerCase(Locale.ROOT);
        if ("arm64-v8a".equals(normalized)) return BOOTSTRAP_PREFIX_ASSET_ROOT + "/arm64-v8a";
        if ("armeabi-v7a".equals(normalized) || "armeabi".equals(normalized)) return BOOTSTRAP_PREFIX_ASSET_ROOT + "/armeabi-v7a";
        if ("x86_64".equals(normalized)) return BOOTSTRAP_PREFIX_ASSET_ROOT + "/x86_64";
        if ("x86".equals(normalized)) return BOOTSTRAP_PREFIX_ASSET_ROOT + "/x86";
        return null;
    }

    private boolean assetDirectoryExists(String assetPath) throws IOException {
        String[] children = assets.list(assetPath);
        return children != null && children.length > 0;
    }

    private boolean shouldBeExecutable(String relativePath) {
        if (relativePath == null || relativePath.isEmpty()) return false;
        String normalized = relativePath.replace('\\', '/');
        return normalized.startsWith("bin/") || normalized.startsWith("libexec/") || normalized.startsWith("sbin/");
    }
}
