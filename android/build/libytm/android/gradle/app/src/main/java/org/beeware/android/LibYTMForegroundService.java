    package org.beeware.android; 
    import android.app.Notification;
    import android.app.NotificationChannel;
    import android.app.NotificationManager;
    import android.app.PendingIntent;
    import android.app.Service;
    import android.content.Context;
    import android.content.Intent;
    import android.os.Build;
    import android.os.IBinder;
    import android.util.Log;
    import androidx.core.app.NotificationCompat;

    import com.chaquo.python.PyObject;
    import com.chaquo.python.Python;
    import com.chaquo.python.android.AndroidPlatform;

    public class LibYTMForegroundService extends Service {

        private static final String CHANNEL_ID = "LibYTMServerChannel";
        private static final int NOTIFICATION_ID = 1;
        private Thread serverThread = null;
        private PyObject mainModule = null;
        private PyObject serverInstance = null;

        @Override
        public void onCreate() {
            super.onCreate();
            Log.d("LibYTMService", "Service onCreate");
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            createNotificationChannel();
        }

        @Override
        public int onStartCommand(Intent intent, int flags, int startId) {
            Log.d("LibYTMService", "Service onStartCommand");

            // --- Create Notification ---
            // Intent to launch the app when notification is tapped
             // Use the correct activity name for your app
            Intent notificationIntent = new Intent(this, MainActivity.class); 
            PendingIntent pendingIntent = PendingIntent.getActivity(this, 0, notificationIntent, PendingIntent.FLAG_IMMUTABLE);

            Notification notification = new NotificationCompat.Builder(this, CHANNEL_ID)
                    .setContentTitle("LibYTM Server")
                    .setContentText("Server is running in the background")
                    //.setSmallIcon(R.mipmap.ic_launcher) 
                    .setContentIntent(pendingIntent)
                    .setOngoing(true) 
                    .build();
                 startForeground(NOTIFICATION_ID, notification);
            if (serverThread == null || !serverThread.isAlive()) {
                serverThread = new Thread(this::startPythonServer);
                serverThread.start();
                Log.d("LibYTMService", "Server thread started");
            } else {
                Log.d("LibYTMService", "Server thread already running");
            }

            return START_STICKY; 
        }

        private void startPythonServer() {
            try {
                Log.d("LibYTMService", "Attempting to start Python server...");
                Python py = Python.getInstance();
                mainModule = py.getModule("libytm.app");
                 PyObject runServerFunc = mainModule.get("run_server_static"); 
                 if (runServerFunc != null) {
                    runServerFunc.call(); // Call the static version
                    Log.d("LibYTMService", "Python server started via static function.");
                 } else {
                    Log.e("LibYTMService", "Could not find run_server_static function in Python module.");
                 }

            } catch (Exception e) {
                Log.e("LibYTMService", "Error starting Python server", e);
                 stopSelf();
            }
        }


        @Override
        public void onDestroy() {
            super.onDestroy();
            Log.d("LibYTMService", "Service onDestroy");
            if (serverThread != null && serverThread.isAlive()) {
                serverThread.interrupt(); 
            }
            serverThread = null;
            stopForeground(true);
        }

        @Override
        public IBinder onBind(Intent intent) {
            // We don't provide binding, so return null
            return null;
        }

        private void createNotificationChannel() {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                NotificationChannel serviceChannel = new NotificationChannel(
                        CHANNEL_ID,
                        "LibYTM Server Channel",
                        NotificationManager.IMPORTANCE_DEFAULT // Low/Default importance is fine
                );
                serviceChannel.setDescription("Channel for LibYTM background server");

                NotificationManager manager = getSystemService(NotificationManager.class);
                if (manager != null) {
                    manager.createNotificationChannel(serviceChannel);
                    Log.d("LibYTMService", "Notification channel created");
                } else {
                     Log.e("LibYTMService", "Failed to get NotificationManager");
                }
            }
        }
    }