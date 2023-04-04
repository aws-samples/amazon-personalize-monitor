# Amazon Personalize Monitor - Campaign Provisioned TPS Update Function

This Lambda function adjusts the `minProvisionedTPS` value for a Personalize campaign or the `minRecommendationRequestsPerSecond` for a Personalize recommender. It is called as the target of EventBridge rules for events emitted by the [personalize_monitor](../personalize_monitor_function/) function when configured to update campaigns and recommenders based on actual TPS activity. You can also incorporate this function into your own operations to scale campaigns and recommenders up and down. For example, if you know your campaign or recommender will experience a massive spike in requests at a certain time (i.e. flash sale) and you want to pre-warm your campaign or recommender capacity, you can create a [CloudWatch event](https://docs.aws.amazon.com/AmazonCloudWatch/latest/events/RunLambdaSchedule.html) to call this function 30 minutes before the expected spike in traffic to increase endpoint capacity and then again after the traffic event to lower the capacity. Alternatively, if there are certain events that occur in your application that you know will generate a predictably higher or lower volume of requests than the current `minProvisionedTPS`/`minRecommendationRequestsPerSecond` **AND** Personalize's auto-scaling will not suffice, you can use this function as a trigger to adjust `minProvisionedTPS`/`minRecommendationRequestsPerSecond` accordingly.

## How it works

The EventBridge event structure that triggers this function for a camapaign looks something like this:

```javascript
{
    "source": "personalize.monitor",
    "detail-type": "UpdatePersonalizeCampaignMinProvisionedTPS",
    "resources": [ CAMPAIGN_ARN_TO_UPDATE ],
    "detail": {
        "ARN": CAMPAIGN_ARN_TO_UPDATE,
        "Utilization": CURRENT_UTILIZATION,
        "AgeHours": CAMPAIGN_AGE_IN_HOURS,
        "CurrentMinTPS": CURRENT_MIN_PROVISIONED_TPS,
        "NewMinTPS": NEW_MIN_PROVISIONED_TPS,
        "MinAverageTPS": MIN_AVERAGE_TPS_LAST_24_HOURS,
        "MaxAverageTPS": MAX_AVERATE_TPS_LAST_24_HOURS,
        "Datapoints": [ CW_METRIC_DATAPOINTS_LAST_24_HOURS ],
        "Reason": DESCRIPTIVE_REASON_FOR_UPDATE
    }
}
```

Similarly, the EventBridge event structure that triggers this function for a recommender looks something like this:

```javascript
{
    "source": "personalize.monitor",
    "detail-type": "UpdatePersonalizeRecommenderMinRecommendationRPS",
    "resources": [ RECOMMENDER_ARN_TO_UPDATE ],
    "detail": {
        "ARN": RECOMMENDER_ARN_TO_UPDATE,
        "Utilization": CURRENT_UTILIZATION,
        "AgeHours": RECOMMENDER_AGE_IN_HOURS,
        "CurrentMinTPS": CURRENT_MIN_RECOMMENDATION_RPS,
        "NewMinTPS": NEW_MIN_RECOMMENDATION_RPS,
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
    "ARN": "CAMPAIGN_OR_RECOMMENDER_ARN_HERE",
    "NewMinTPS": NEW_MIN_TPS_HERE,
    "Reason": DESCRIPTIVE_REASON_FOR_UPDATE
}
```

For Personalize campaigns, the [UpdateCampaign](https://docs.aws.amazon.com/personalize/latest/dg/API_UpdateCampaign.html) API is used to update the `minProvisionedTPS` value. For Personalize recommenders, the [UpdateRecommender](https://docs.aws.amazon.com/personalize/latest/dg/API_UpdateRecommender.html) API is used to update the `minRecommendationRequestsPerSecond` value.

## Published events

When an update of a campaign's `minProvisionedTPS` or recommender's `minRecommendationRequestsPerSecond` has been successfully initiated by this function, an event is published to EventBridge to trigger a notification.

> Since it can take several minutes for a campaign or recommender to redeploy after updating its `minProvisionedTPS` or `minRecommendationRequestsPerSecond`, you will receive the notification when the redeploy starts. The campaign/recommender will continue to respond to `GetRecommendations`/`GetPersonalizedRanking` API requests while it is redeploying. **Therefore, there will be no interruption of service while it's redeploying.**

### Update minProvisionedTPS notification

The following event is published to EventBridge to signal that an update to a campaign has been initiated.

```javascript
{
    "source": "personalize.monitor",
    "detail_type": "PersonalizeCampaignMinProvisionedTPSUpdated",
    "resources": [ CAMPAIGN_ARN_UPDATED ],
    "detail": {
        "ARN": CAMPAIGN_ARN_UPDATED,
        "NewMinTPS": NEW_TPS,
        "Reason": DESCRIPTIVE_REASON_FOR_DELETE
    }
}
```

### Update minRecommendationRequestsPerSecond notification

The following event is published to EventBridge to signal that an update to a recommender has been initiated.

```javascript
{
    "source": "personalize.monitor",
    "detail_type": "PersonalizeRecommenderMinRecommendationRPSUpdated",
    "resources": [ RECOMMENDER_ARN_UPDATED ],
    "detail": {
        "ARN": RECOMMENDER_ARN_UPDATED,
        "NewMinTPS": NEW_TPS,
        "Reason": DESCRIPTIVE_REASON_FOR_DELETE
    }
}
```

An EventBridge rule is setup that will target an SNS topic with `NotificationEndpoint` as the subscriber. This is the email address you provided at deployment time. If you'd like, you can customize how these notification events are handled or add your own targets in the EventBridge and SNS consoles.
