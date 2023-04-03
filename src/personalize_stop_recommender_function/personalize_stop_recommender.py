# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function that is used to stop a Personalize recommender based on prolonged idle time
and according to configuration to automatically stop recommenders under these conditions.
Note that this function just stops the recommender; it does NOT delete the recommender. The
idea is to stop ongoing charges for an idle recommender.
"""

import json
import logging

from aws_lambda_powertools import Logger

from common import (
    PROJECT_NAME,
    ALARM_NAME_PREFIX,
    extract_region,
    get_client,
    put_event
)

logger = Logger()

def delete_alarms_for_recommender(recommender_arn):
    cw = get_client(service_name = 'cloudwatch', region_name = extract_region(recommender_arn))

    alarm_names_to_delete = set()

    alarms_paginator = cw.get_paginator('describe_alarms')
    for alarms_page in alarms_paginator.paginate(AlarmNamePrefix = ALARM_NAME_PREFIX, AlarmTypes=['MetricAlarm']):
        for alarm in alarms_page['MetricAlarms']:
            for dim in alarm['Dimensions']:
                if dim['Name'] == 'RecommenderArn' and dim['Value'] == recommender_arn:
                    tags_response = cw.list_tags_for_resource(ResourceARN = alarm['AlarmArn'])

                    for tag in tags_response['Tags']:
                        if tag['Key'] == 'CreatedBy' and tag['Value'] == PROJECT_NAME:
                            alarm_names_to_delete.add(alarm['AlarmName'])
                            break

    if alarm_names_to_delete:
        # FUTURE: max check of 100
        logger.info('Deleting CloudWatch alarms for recommender %s: %s', recommender_arn, alarm_names_to_delete)
        cw.delete_alarms(AlarmNames=list(alarm_names_to_delete))
        alarms_deleted += len(alarm_names_to_delete)
    else:
        logger.info('No CloudWatch alarms to delete for recommender %s', recommender_arn)

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, _):
    ''' Initiates stopping a Personalize recommender '''
    if event.get('detail'):
        recommender_arn = event['detail']['ARN']
        reason = event['detail'].get('Reason')
    else:
        recommender_arn = event['ARN']
        reason = event.get('Reason')

    region = extract_region(recommender_arn)
    if not region:
        raise Exception('Region could not be extracted from ARN')

    personalize = get_client(service_name = 'personalize', region_name = region)

    response = personalize.stop_recommender(recommenderArn = recommender_arn)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(json.dumps(response, indent = 2, default = str))

    if not reason:
        reason = f'Amazon Personalize recommender {recommender_arn} stop initiated (reason unspecified)'

    put_event(
        detail_type = 'PersonalizeRecommenderStopped',
        detail = json.dumps({
            'ARN': recommender_arn,
            'Reason': reason
        }),
        resources = [ recommender_arn ]
    )

    put_event(
        detail_type = 'BuildPersonalizeMonitorDashboard',
        detail = json.dumps({
            'ARN': recommender_arn,
            'Reason': reason
        }),
        resources = [ recommender_arn ]
    )

    logger.info({
        'recommenderArn': recommender_arn
    })

    delete_alarms_for_recommender(recommender_arn)

    return f'Successfully initiated stop of recommender {recommender_arn}'