# Amazon Personalize Monitor - Stop Recommender Function

This Lambda function stops a Personalize recommender. It is called as the target of an EventBridge rule that matches events with the `StopPersonalizeRecommender` detail-type. The [personalize-monitor](../personalize_monitor_function/) function publishes this event when the `AutoDeleteOrStopIdleResources` deployment parameter is `Yes` AND a monitored recommender has been idle more than `IdleThresholdHours` hours. Therefore, an idle recommender is one that has not had any `GetRecommendations` calls in the last `IdleThresholdHours` hours.

This function will also delete any CloudWatch alarms that were dynamically created by this application for the stopped recommender. Alarms can be created for idle recommenders and low utilization recommenders via the `AutoCreateIdleAlarms` and `AutoCreateUtilizationAlarms` deployment parameters.

> Note that Personalize campaigns are deleted and not stopped by this application. Since model artifacts are associated with a solution version, deleting a campaign does **not** delete the actual model artifacts. See the [personalize_delete_campaign](../personalize_delete_campaign_function/) function for details.

## How it works

The EventBridge event structure that triggers this function looks something like this:

```javascript
{
    "source": "personalize.monitor",
    "detail-type": "StopPersonalizeRecommender",
    "resources": [ RECOMMENDER_ARN_TO_STOP ],
    "detail": {
        'ARN': RECOMMENDER_ARN_TO_STOP,
        'Utilization': CURRENT_UTILIZATION,
        'AgeHours': RECOMMENDER_AGE_IN_HOURS,
        'IdleThresholdHours': RECOMMENDER_IDLE_HOURS,
        'TotalRequestsDuringIdleThresholdHours': 0,
        'Reason': DESCRIPTIVE_REASON_FOR_DELETE
    }
}
```

This function can also be invoked directly as part of your own operational process. The event you pass to the function only requires the recommender ARN as follows.

```javascript
{
    "ARN": RECOMMENDER_ARN_TO_STOP,
    "Reason": OPTIONAL_DESCRIPTIVE_REASON_FOR_DELETE
}
```

The Personalize [StopRecommender](https://docs.aws.amazon.com/personalize/latest/dg/API_StopRecommender.html) API is used to stop the recommender.

## Published events

When the recommender stop request and the deletion of any dynamically created CloudWatch alarms for the recommender have been successfully initiated by this function, two events are published to EventBridge. One event will trigger a notification to the SNS topic for this application and the other trigger the CloudWatch dashboard to be rebuilt.

### Delete notification

The following event is published to EventBridge to signal that a campaign has been deleted.

```javascript
{
    "source": "personalize.monitor",
    "detail_type": "PersonalizeRecommenderStopped",
    "resources": [ RECOMMENDER_ARN_STOPPED ],
    "detail": {
        "ARN": RECOMMENDER_ARN_STOPPED,
        "Reason": DESCRIPTIVE_REASON_FOR_STOP
    }
}
```

An EventBridge rule is setup that will target an SNS topic with `NotificationEndpoint` as the subscriber. This is the email address you provided at deployment time. If you'd like, you can customize how these notification events are handled in the EventBridge and SNS consoles.

### Rebuild CloudWatch dashboard

Since a monitored recommender has been stopped, the CloudWatch dashboard needs to be rebuilt so that the recommender is removed from the widgets. This is accomplished by publishing a `BuildPersonalizeMonitorDashboard` event that is processed by the [dashboard_mgmt](../dashboard_mgmt_function/) function.

```javascript
{
    "source": "personalize.monitor",
    "detail_type": "BuildPersonalizeMonitorDashboard",
    "resources": [ RECOMMENDER_ARN_STOPPED ],
    "detail": {
        "ARN": RECOMMENDER_ARN_STOPPED,
        "Reason": DESCRIPTIVE_REASON_FOR_REBUILD
    }
}
```
