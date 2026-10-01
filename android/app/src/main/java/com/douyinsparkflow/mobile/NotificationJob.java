package com.douyinsparkflow.mobile;

import android.app.job.JobParameters;
import android.app.job.JobService;

public final class NotificationJob extends JobService {
    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            NotificationPoller.poll(getApplicationContext());
            jobFinished(params, false);
        }, "send-result-poll").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;
    }
}
