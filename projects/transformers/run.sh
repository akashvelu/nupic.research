#!/bin/bash

: '
Two ways of running this script:

1 - There is a single config file and RAY_CONFIG_FILE is an existing environment variable

    This is the prefered way. You can set the environment variable prior to running the script by running:
    export RAY_CONFIG_FILE=<path to you ray config file>

    In this scenario, the script only takes one argument:
    ./run.sh <experiment_name>

    Wait a minute or two. The output of all instances will be redirected to the local terminal.

2 - There are multiple config files and RAY_CONFIG_FOLDER is an existing environment variable

    In may be the case you have multiple ray config files.
    RAY_CONFIG_FILE should be non-existing or set to empty string.
    RAY_CONFIG_FOLDER should be defined instead.

    In this scenario, the script takes two arguments:
    ./run.sh <cluster_number> <experiment_name>

    This script assumes the config files follow the naming convention:
    ray_user<cluster_number>.yaml

    If you would like to use different filename, make a local copy of the run script

Both scenarios require a AWS_CERT_FILE environment variable with the path to the .pem file
export AWS_CERT_FILE=<path to your AWS certificatin file (.pem)>

Set the environment variables in ~/.bash_profile to load it every time a new bash terminal is open
'

aws_ec2_user="ec2-user"
run_file="~/nta/nupic.research/projects/transformers/run.py"

# ------ Find config file -------------

if [ -z "$RAY_CONFIG_FILE" ]
then
    ray_config_file="$RAY_CONFIG_FOLDER/ray_user$1.yaml"
    exp_name=$2
else
    ray_config_file=$RAY_CONFIG_FILE
    exp_name=$1
fi

echo "Ray config file: " $ray_config_file
echo "Experiment name: " $exp_name


# ------ Get head private IP -------------

get_head_private_ip() {
    local ip=$(ssh -o "StrictHostKeyChecking no" -i \
              "$AWS_CERT_FILE" \
              "$aws_ec2_user"@$(ray get-head-ip "$ray_config_file") \
              hostname -I | awk '{print $1}')
    echo $ip
}

head_private_ip=$(get_head_private_ip $1)
echo "Head private IP: $head_private_ip"

# ------ Get head and workers public IP -------------

head_public_ip=$(ray get-head-ip "$ray_config_file")
echo "Head public IP: $head_public_ip"

get_worker_public_ips() {
    local counter=0
    for ip in $(ray get-worker-ips "$ray_config_file")
    do
        (( counter++ ))
        echo $ip
    done
}

worker_public_ips=$(get_worker_public_ips $1)
echo "Worker public IPs: ${worker_public_ips}"

# ------ Get number of active workers -------------

num_workers=$(echo "$worker_public_ips" | wc -w)
echo "Number of (max) workers: $num_workers"

# ------ Get number of GPUs -------------

get_num_gpus() {
    local instance_type=$(cat "$ray_config_file" | grep "  InstanceType" | sed -n '2p' | awk '{print $2}')
    case $instance_type in
        p3.2xlarge)
            echo 1
            ;;
        p3.8xlarge)
            echo 4
            ;;
        p3.16xlarge)
            echo 8
            ;;
        p3dn.24xlarge)
            echo 8
            ;;
        *)
            echo 0
            ;;
    esac
}

num_gpus=$(get_num_gpus $1)
echo "Number of GPUS per instance: $num_gpus"

# ------ Generate a random port number -------------
# Port number in interval [1200, 1300].
# TODO: verify if port is available, if not select another

random_port=$(( $RANDOM % 100 + 1200 ))
echo "Random port selected: $random_port"

# ------ Run file -------------

run_experiment() {

    local counter=0
    local all_ips="${head_public_ip} ${worker_public_ips}"
    for ip in $all_ips
    do
        echo "Running command in instance $counter : $ip"

        ssh -o "StrictHostKeyChecking no" -i \
        "$AWS_CERT_FILE" \
        "$aws_ec2_user"@$ip \
        python -m torch.distributed.launch \
            --nproc_per_node $num_gpus \
            --nnodes $(( num_workers+1 )) \
            --node_rank $counter \
            --master_addr $head_private_ip \
            --master_port $random_port \
            "$run_file" \
            --experiment $1 &

        (( counter++ ))
    done

}

run_experiment $exp_name
