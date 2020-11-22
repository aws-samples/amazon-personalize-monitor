# Amazon Personalize Monitor - Delete Campaign Function

This Lambda function deletes a Personalize campaign. It is called as the target of an EventBridge rule that matches events with the `DeletePersonalizeCampaign` detail-type. The [personalize-monitor](../personalize_monitor_function/) function publishes this event when the `AutoDeleteIdleCampaigns` deployment parameter is `Yes` AND a monitored campaign has been idle more than `IdleCampaignThresholdHours` hours. Therefore, an idle campaign is one that has not had any `GetRecommendations` or `GetPersonalizedRanking` calls in the last `IdleCampaignThresholdHours` hours.

This function will also delete any CloudWatch alarms that were dynamically created by this application for the deleted campaign. Alarms can be created for idle campaigns and low utilization campaigns via the `AutoCreateIdleCampaignAlarms` and `AutoCreateCampaignUtilizationAlarms` deployment parameters.

## How it works

The EventBridge event structure that triggers this function looks something like this:

```javascript
{
    "source": "personalize.monitor",
    "detail-type": "DeletePersonalizeCampaign",
    "resources": [ CAMPAIGN_ARN_TO_DELETE ],
    "detail": {
        'CampaignARN': CAMPAIGN_ARN_TO_DELETE,
        'CampaignUtilization': CURRENT_UTILIZATION,
        'CampaignAgeHours': CAMPAIGN_AGE_IN_HOURS,
        'IdleCampaignThresholdHours': CAMPAIGN_IDLE_HOURS,
        'TotalRequestsDuringIdleThresholdHours': 0,
        'Reason': DESCRIPTIVE_REASON_FOR_DELETE
    }
}
```

This function can also be invoked directly as part of your own operational process. The event you pass to the function only requires the campaign ARN as follows. 

```javascript
{
    "CampaignARN": CAMPAIGN_ARN_TO_DELETE,
    "Reason": OPTIONAL_DESCRIPTIVE_REASON_FOR_DELETE
}
```

The Personalize [DeleteCampaign](https://docs.aws.amazon.com/personalize/latest/dg/API_DeleteCampaign.html) API is used to delete the campaign.

## Published events

When the deletion of a campaign and any dynamically created CloudWatch alarms for the campaign have been successfully initiated by this function, two events are published to EventBridge. One event will trigger a notification to the SNS topic for this application and the other trigger the CloudWatch dashboard to be rebuilt.

### Delete notification

The following event is published to EventBridge to signal that a campaign has been deleted.

```javascript
{
    "source": "personalize.monitor",
    "detail_type": "PersonalizeCampaignDeleted",
    "resources": [ CAMPAIGN_ARN_DELETED ],
    "detail": {
        "CampaignARN": CAMPAIGN_ARN_DELETED,
        "Reason": DESCRIPTIVE_REASON_FOR_DELETE
    }
}
```

An EventBridge rule is setup that will target an SNS topic with `NotificationEndpoint` as the subscriber. This is the email address you provided at deployment time. If you'd like, you can customize how these notification events are handled in the EventBridge and SNS consoles.

### Rebuild CloudWatch dashboard

Since a monitored campaign has been deleted, the CloudWatch dashboard needs to be rebuilt so that the campaign is removed from the widgets. This is accomplished by publishing a `BuildPersonalizeMonitorDashboard` event that is processed by the [dashboard_mgmt](../dashboard_mgmt_function/) function.

```javascript
{
    "source": "personalize.monitor",
    "detail_type": "BuildPersonalizeMonitorDashboard",
    "resources": [ CAMPAIGN_ARN_DELETED ],
    "detail": {
        "CampaignARN": CAMPAIGN_ARN_DELETED,
        "Reason": DESCRIPTIVE_REASON_FOR_REBUILD
    }
}
```
