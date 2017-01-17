#!/bin/bash -ex

export LIBGUESTFS_BACKEND=direct

# ensure /dev/kvm exists, otherwise it will still use
# direct backend, but without KVM(much slower).
! [[ -c "/dev/kvm" ]] && mknod /dev/kvm c 10 232


DISTRO='el7'
AUTOMATION="$PWD"/automation
PREFIX="$AUTOMATION"/vdsm_functional
EXPORTS="$PWD"/exported-artifacts


# Creates RPMS
"$AUTOMATION"/build-artifacts.sh

if [[ -d "$PREFIX" ]]; then
    pushd "$PREFIX"
    echo 'cleaning old lago env'
    lago cleanup || :
    popd
    rm -rf "$PREFIX"
fi

# Fix when running in an el* chroot in fc2* host
[[ -e /usr/bin/qemu-kvm ]] \
|| ln -s /usr/libexec/qemu-kvm /usr/bin/qemu-kvm

lago init \
    "$PREFIX" \
    "$AUTOMATION"/lago-env.yml

cd "$PREFIX"
lago ovirt reposetup \
    --reposync-yum-config /dev/null \
    --custom-source "dir:$EXPORTS"

function fake_ksm_in_vm {
    lago shell "$vm_name" -c "mount -t tmpfs tmpfs /sys/kernel/mm/ksm"
}

function run_infra_tests {
    local res=0
    lago shell "$vm_name" -c \
        " \
            cd /usr/share/vdsm/tests
            ./run_tests.sh \
                --with-xunit \
                --xunit-file=/tmp/nosetests-${DISTRO}.xml \
                -s \
                functional/supervdsmFuncTests.py \
                functional/upgrade_vdsm_test.py \
        " || res=$?
    return $res
}

function run_network_tests {
    local res=0
    lago shell "$vm_name" -c \
        " \
            systemctl stop NetworkManager
            systemctl mask NetworkManager
            cd /usr/share/vdsm/tests
            ./run_tests.sh \
                -a type=functional,switch=legacy \
                network/func_*_test.py
        " || res=$?
    return $res
}

function prepare_and_copy_yum_conf {
    local vm_name="$1"
    local tempfile=$(mktemp XXXXXX)

    cat /etc/yum/yum.conf 2>/dev/null | \
    grep -v "reposdir" | \
    "$AUTOMATION"/exclude_from_conf 'vdsm*' > "$tempfile"

    lago copy-to-vm "$vm_name" "$tempfile" /etc/yum/yum.conf
    rm "$tempfile"
}

mkdir "$EXPORTS"/lago-logs
failed=0

vm_name="vdsm_functional_tests_host-${DISTRO}"
lago start "$vm_name"

prepare_and_copy_yum_conf "$vm_name"

# the ovirt deploy is needed because it will not start the local repo
# otherwise
lago ovirt deploy

lago ovirt serve &
PID=$!

fake_kvm_in_vm

run_infra_tests | tee "$EXPORTS/functional_tests_stdout.$DISTRO.log"
failed="${PIPESTATUS[0]}"

run_network_tests | tee -a "$EXPORTS/functional_tests_stdout.$DISTRO.log"
res="${PIPESTATUS[0]}"
[ "$res" -ne 0 ] && failed="$res"

kill $PID

lago copy-from-vm \
"$vm_name" \
"/tmp/nosetests-${DISTRO}.xml" \
"$EXPORTS/nosetests-${DISTRO}.xml" || :
lago collect --output "$EXPORTS"/lago-logs
lago stop "$vm_name"

lago cleanup

cp "$PREFIX"/current/logs/*.log "$EXPORTS"/lago-logs

exit $failed
