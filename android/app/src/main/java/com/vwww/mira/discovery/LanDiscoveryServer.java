package com.vwww.mira.discovery;

import android.content.Context;
import android.net.wifi.WifiManager;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

public final class LanDiscoveryServer implements Closeable {
    private static final String TAG = "MiraDiscovery";

    public interface Callback {
        JSONObject deviceMetadata(String wakeUrl) throws JSONException;

        boolean openSession(JSONObject request);
    }

    private final Context context;
    private final int requestedDiscoveryPort;
    private final String installId;
    private final Callback callback;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final AtomicInteger lifecycleGeneration = new AtomicInteger(0);
    private final Set<Socket> clients = Collections.synchronizedSet(new HashSet<>());

    private DatagramSocket udpSocket;
    private ServerSocket wakeServer;
    private WifiManager.MulticastLock multicastLock;
    private ExecutorService clientExecutor;
    private int discoveryPort;
    private int wakePort;
    private boolean closed;

    public LanDiscoveryServer(Context context, int discoveryPort, String installId, Callback callback) {
        Context applicationContext = context.getApplicationContext();
        this.context = applicationContext == null ? context : applicationContext;
        this.requestedDiscoveryPort = discoveryPort;
        this.installId = installId;
        this.callback = callback;
    }

    public synchronized void start() throws IOException {
        if (running.get()) return;
        if (closed) throw new IllegalStateException("LanDiscoveryServer cannot be restarted after close");

        int generation = lifecycleGeneration.incrementAndGet();
        ServerSocket nextWakeServer = null;
        DatagramSocket nextUdpSocket = null;
        WifiManager.MulticastLock nextMulticastLock = null;
        ExecutorService nextClientExecutor = null;
        try {
            nextMulticastLock = acquireMulticastLock();

            nextWakeServer = new ServerSocket();
            nextWakeServer.setReuseAddress(true);
            nextWakeServer.bind(new InetSocketAddress(InetAddress.getByName("0.0.0.0"), 0), 16);

            nextUdpSocket = new DatagramSocket(null);
            nextUdpSocket.setReuseAddress(true);
            nextUdpSocket.setBroadcast(true);
            nextUdpSocket.bind(new InetSocketAddress(requestedDiscoveryPort));
            nextClientExecutor = Executors.newCachedThreadPool();
        } catch (IOException | RuntimeException exception) {
            closeDatagramSocketQuietly(nextUdpSocket);
            closeQuietly(nextWakeServer);
            releaseMulticastLock(nextMulticastLock);
            if (nextClientExecutor != null) nextClientExecutor.shutdownNow();
            lifecycleGeneration.incrementAndGet();
            throw exception;
        }

        multicastLock = nextMulticastLock;
        wakeServer = nextWakeServer;
        udpSocket = nextUdpSocket;
        clientExecutor = nextClientExecutor;
        discoveryPort = nextUdpSocket.getLocalPort();
        wakePort = nextWakeServer.getLocalPort();
        running.set(true);

        DatagramSocket currentUdpSocket = nextUdpSocket;
        ServerSocket currentWakeServer = nextWakeServer;
        new Thread(() -> udpLoop(generation, currentUdpSocket, currentWakeServer), "MiraDiscoveryUdp").start();
        new Thread(() -> wakeLoop(generation, currentWakeServer), "MiraDiscoveryWake").start();
        Log.i(TAG, "Discovery started udp=" + discoveryPort + " wake=" + wakePort + " ip=" + localIPv4());
    }

    public synchronized int getDiscoveryPort() {
        return discoveryPort;
    }

    public synchronized int getWakePort() {
        return wakePort;
    }

    @Override
    public synchronized void close() {
        if (closed) return;
        closed = true;
        running.set(false);
        lifecycleGeneration.incrementAndGet();
        closeDatagramSocketQuietly(udpSocket);
        closeQuietly(wakeServer);
        closeClients();
        if (clientExecutor != null) clientExecutor.shutdownNow();
        releaseMulticastLock(multicastLock);
        udpSocket = null;
        wakeServer = null;
        clientExecutor = null;
        multicastLock = null;
    }

    private void udpLoop(int generation, DatagramSocket socket, ServerSocket server) {
        byte[] buffer = new byte[65535];
        while (isCurrent(generation)) {
            try {
                DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                socket.receive(packet);
                String text = new String(packet.getData(), packet.getOffset(), packet.getLength(), StandardCharsets.UTF_8);
                JSONObject request = new JSONObject(text);
                if (!"mira.discover".equals(request.optString("type"))) continue;
                Log.i(TAG, "Discovery request from " + packet.getAddress().getHostAddress() + ":" + packet.getPort());
                String wakeUrl = "http://" + localIPv4() + ":" + server.getLocalPort() + "/session/open";
                JSONObject response = callback.deviceMetadata(wakeUrl);
                if (!isCurrent(generation)) continue;
                byte[] payload = response.toString().getBytes(StandardCharsets.UTF_8);
                DatagramPacket reply = new DatagramPacket(payload, payload.length, packet.getAddress(), packet.getPort());
                socket.send(reply);
                Log.i(TAG, "Discovery response sent to " + packet.getAddress().getHostAddress() + ":" + packet.getPort());
            } catch (Throwable throwable) {
                if (isCurrent(generation)) Log.w(TAG, "Discovery loop error", throwable);
            }
        }
    }

    private void wakeLoop(int generation, ServerSocket server) {
        while (isCurrent(generation)) {
            try {
                Socket socket = server.accept();
                clients.add(socket);
                ExecutorService executor = clientExecutor;
                if (executor == null || !isCurrent(generation)) {
                    closeClient(socket);
                    continue;
                }
                try {
                    executor.execute(() -> handleWakeClient(generation, socket));
                } catch (RejectedExecutionException exception) {
                    closeClient(socket);
                    if (isCurrent(generation)) Log.w(TAG, "Wake request rejected", exception);
                }
            } catch (IOException exception) {
                if (isCurrent(generation)) Log.w(TAG, "Wake loop error", exception);
            }
        }
    }

    private void handleWakeClient(int generation, Socket socket) {
        try (Socket client = socket) {
            client.setTcpNoDelay(true);
            InputStream input = client.getInputStream();
            OutputStream output = client.getOutputStream();
            HttpRequestParser.Request request = HttpRequestParser.read(input);
            if (request == null || !isCurrent(generation)) return;
            if (!"POST".equals(request.method) || !"/session/open".equals(request.path)) {
                HttpRequestParser.writeJson(output, "404 Not Found", new JSONObject().put("error", "not found").toString());
                return;
            }
            JSONObject body = new JSONObject(new String(request.body, StandardCharsets.UTF_8));
            if (!installId.equals(body.optString("installId"))) {
                HttpRequestParser.writeJson(output, "404 Not Found", new JSONObject().put("error", "wrong installId").toString());
                return;
            }
            synchronized (this) {
                if (!isCurrent(generation)) return;
                if (!callback.openSession(body)) {
                    HttpRequestParser.writeJson(output, "409 Conflict", new JSONObject().put("error", "session already active").toString());
                    return;
                }
            }
            if (!isCurrent(generation)) return;
            HttpRequestParser.writeJson(output, "200 OK", new JSONObject().put("ok", true).put("state", "active").toString());
        } catch (Throwable throwable) {
            if (isCurrent(generation)) Log.w(TAG, "Wake request failed", throwable);
        } finally {
            clients.remove(socket);
        }
    }

    private boolean isCurrent(int generation) {
        return running.get() && lifecycleGeneration.get() == generation;
    }

    private WifiManager.MulticastLock acquireMulticastLock() {
        try {
            WifiManager wifiManager = (WifiManager) context.getSystemService(Context.WIFI_SERVICE);
            if (wifiManager == null) return null;
            WifiManager.MulticastLock lock = wifiManager.createMulticastLock("mira-discovery");
            lock.setReferenceCounted(false);
            lock.acquire();
            return lock;
        } catch (Throwable throwable) {
            Log.w(TAG, "Unable to acquire multicast lock", throwable);
            return null;
        }
    }

    private void closeClients() {
        Socket[] snapshot;
        synchronized (clients) {
            snapshot = clients.toArray(new Socket[0]);
            clients.clear();
        }
        for (Socket client : snapshot) closeQuietly(client);
    }

    private void closeClient(Socket client) {
        clients.remove(client);
        closeQuietly(client);
    }

    private static void releaseMulticastLock(WifiManager.MulticastLock lock) {
        try {
            if (lock != null && lock.isHeld()) lock.release();
        } catch (Throwable throwable) {
            Log.w(TAG, "Unable to release multicast lock", throwable);
        }
    }

    private static String localIPv4() {
        try {
            for (NetworkInterface networkInterface : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!networkInterface.isUp() || networkInterface.isLoopback()) continue;
                for (InetAddress address : Collections.list(networkInterface.getInetAddresses())) {
                    if (address instanceof Inet4Address && !address.isLoopbackAddress()) return address.getHostAddress();
                }
            }
        } catch (Exception ignored) {
        }
        return "127.0.0.1";
    }

    private static void closeQuietly(Closeable closeable) {
        if (closeable == null) return;
        try {
            closeable.close();
        } catch (Throwable ignored) {
        }
    }

    private static void closeDatagramSocketQuietly(DatagramSocket socket) {
        if (socket == null) return;
        try {
            socket.close();
        } catch (Throwable ignored) {
        }
    }
}
