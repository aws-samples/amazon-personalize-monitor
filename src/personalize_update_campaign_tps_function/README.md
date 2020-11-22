# Amazon Personalize Monitor - Campaign Provisioned TPS Update Function

This Lambda function adjusts the `minProvisionedTPS` value for a Personalize campaign. It is called as the target of EventBridge rules for events emitted by the [personalize_monitor](../personalize_monitor_function/) function when configured to update campaigns based on actual TPS activity. You can also incorporate this function into your own operations to scale campaigns up and down. For example, if you know your campaign will experience a massive spike in requests at a certain time (i.e. flash sale) and you want to pre-warm your campaign capacity, you can create a [CloudWatch event](https://docs.aws.amazon.com/AmazonCloudWatch/latest/events/RunLambdaSchedule.html) to call this function 30 minutes before the expected spike in traffic to increase the `minProvisionedTPS` and then again after the traffic event to lower the `minProvisionedTPS`. Alternatively, if there are certain events that occur in your application that you know will generate a predictably higher or lower volume of requests than the current `minProvisionedTPS` **AND** Personalize's auto-scaling will not suffice, you can use this function as a trigger to adjust `minProvisionedTPS` accordingly.

## How it works

The EventBridge event structure that triggers this function looks something like this:

```javascript
{
    "source": "personalize.monitor",
    "detail-type": "UpdatePersonalizeCampaignMinProvisionedTPS",
    "resources": [ CAMPAIGN_ARN_TO_UPDATE ],
    "detail": {
        "CampaignARN": CAMPAIGN_ARN_TO_UPDATE,
        "CampaignUtilization": CURRENT_UTILIZATION,
        "CampaignAgeHours": CAMPAIGN_AGE_IN_HOURS,
        "CurrentProvisionedTPS": CURRENT_MIN_PROVISIONED_TPS,
        "MinProvisionedTPS": NEW_MIN_PROVISIONED_TPS,
        "MinAverageTPS": MIN_AVERAGE_TPS_LAST_24_HOURS,
        "MaxAverageTPS": MAX_AVERATE_TPS_LAST_24_HOURS,
        "Datapoints": [ CW_METRIC_DATAPOINTS_LAST_24_HOURS ],
        "Reason": DESCRIPTIVE_REASON_FOR_UPDATE
    }
}
```

This function can also be invoked directly as part of your own operational process. The event you pass to the function only requires the campaign ARN and new `minProvisionedTPS` as follows. 

```javascript
{
    "CampaignARN": "CAMPAIGN_ARN_HERE",
    "MinProvisionedTPS": NEW_MIN_PROVISIONED_TPS_HERE,
    "Reason": DESCRIPTIVE_REASON_FOR_UPDATE
}
```

The Personalize [UpdateCampaign](https://docs.aws.amazon.com/personalize/latest/dg/API_UpdateCampaign.html) API is used to update the `minProvisionedTPS` value.

## Published events

When an update of a campaign's `minProvisionedTPS` has been successfully initiated by this function, an event is published to EventBridge to trigger a notification.

> Since it can take several minutes for a campaign to redeploy after updating its `minProvisionedTPS`, you will receive the notification when the redeploy starts. The campaign will continue to respond to `GetRecommendations`/`GetPersonalizedRanking` API requests while it is redeploying. **Therefore, there will be no interruption of service.**

### Update minProvisionedTPS notification

The following event is published to EventBridge to signal that an update to a campaign has been initiated.

```javascript
{
    "source": "personalize.monitor",
    "detail_type": "PersonalizeCampaignMinProvisionedTPSUpdated",
    "resources": [ CAMPAIGN_ARN_UPDATED ],
    "detail": {
        "CampaignARN": CAMPAIGN_ARN_UPDATED,
        "NewMinProvisionedTPS": NEW_TPS,
        "Reason": DESCRIPTIVE_REASON_FOR_DELETE
    }
}
```

An EventBridge rule is setup that will target an SNS topic with `NotificationEndpoint` as the subscriber. This is the email address you provided at deployment time. If you'd like, you can customize how these notification events are handled or add your own targets in the EventBridge and SNS consoles.
